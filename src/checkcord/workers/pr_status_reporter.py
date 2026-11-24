import asyncio
import json
import time
import traceback
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.code.providers.github.parser import OWN_BRANCH_PREFIX
from checkcord.adapters.code.providers.github.rest_client import GitHubRestClient
from checkcord.bus import pending
from checkcord.bus.dead_letter import write_dead_letter
from checkcord.core import redaction
from checkcord.core.config import get_settings
from checkcord.core.viewer_links import build_node_url
from checkcord.llm import usage
from checkcord.llm.client import LLMClient, LLMTask
from checkcord.llm.factory import build_chat_model
from checkcord.models.enums import Severity, SourcePlatform
from checkcord.models.event import CanonicalEvent
from checkcord.models.pr_checks import AffectedContext, PRContextCheckedPayload
from checkcord.services import record_notes
from checkcord.services.github_tokens import github_token_for
from checkcord.services.repo_audience import RepoAudience, resolve_repo_audience
from checkcord.storage import audit
from checkcord.storage.models import CanonicalContext, ContextEventLink
from checkcord.storage.postgres import (
    PostgresEventStore,
    _scope_filter,
    decisions_or_blocking,
    events_table,
    live_records,
    tenant_context,
)

logger = structlog.get_logger(__name__).bind(component="pr_status_reporter")

TOPIC = "ci.pr.context_checked"
RETRY_ZSET = "ci.pr.retry_zset"
COMMENT_MARKER = "<!-- checkcord:pr-check -->"
SCOPE_CANDIDATES = 20
JUDGE_ATTEMPTS = 3
RETRY_SECONDS = 60
CHECK_DEADLINE_SECONDS = 900
CHECKING = "Checkcord is checking this change."
NOT_EVALUATED = (
    "Checkcord could not evaluate this change. Push again or re-deliver the webhook to retry."
)
ATTACHED_SHOWN = 5
COMMENT_TEXT_CHARS = 300
ATTACHED_TEXT_CHARS = 120
CONFLICTS, COMPLIES, UNRELATED, FAILED, ATTACHED = (
    "conflicts",
    "complies",
    "unrelated",
    "failed",
    "attached",
)
_PLATFORM_NAMES = {
    "slack": "Slack thread",
    "jira": "Jira ticket",
    "ms_teams": "Teams chat",
    "github": "GitHub event",
}


def not_evaluated() -> tuple[str, str]:
    """The status for a change no verdict was reached on."""
    if get_settings().pr_check_on_error == "success":
        return "success", "Not evaluated: internal error."
    return "error", NOT_EVALUATED


async def post_not_evaluated(
    redis: Redis, db_engine: AsyncEngine | None, tenant_id: UUID, repo_name: str, sha: str
) -> str:
    owner, repo_short = repo_name.split("/", 1)
    state, description = not_evaluated()
    client = GitHubRestClient(await github_token_for(db_engine, tenant_id, repo_name))
    await client.create_commit_status(
        owner=owner, repo=repo_short, sha=sha, state=state, description=description
    )
    await redis.set(f"ci:pr_status:{sha}", "completed")
    return state


def _day(value: str | None) -> str:
    return (value or "")[:10]


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _build_source_link(event: CanonicalEvent) -> str | None:
    """Build a direct discussion/ticket hyperlink for the originating event."""
    try:
        chat_source = event.metadata.get("chat_source")
        if isinstance(chat_source, dict) and chat_source.get("url"):
            return str(chat_source["url"])

        platform = (
            event.source_platform.value
            if hasattr(event.source_platform, "value")
            else str(event.source_platform)
        ).lower()

        if platform == SourcePlatform.SLACK.value:
            channel_id = event.metadata.get("channel_id")
            ts = event.source_id
            if channel_id and ts:
                ts_clean = ts.replace(".", "")
                return f"https://slack.com/archives/{channel_id}/p{ts_clean}"

        elif platform == SourcePlatform.JIRA.value:
            html_url = event.metadata.get("html_url")
            if html_url:
                return str(html_url)

        elif platform == SourcePlatform.MS_TEAMS.value:
            web_url = event.source_raw_payload.get("webUrl") or event.metadata.get("web_url")
            if web_url:
                return str(web_url)

        elif platform == SourcePlatform.GITHUB.value:
            html_url = event.metadata.get("html_url") or event.source_raw_payload.get("html_url")
            if not html_url and "pull_request" in event.source_raw_payload:
                html_url = event.source_raw_payload["pull_request"].get("html_url")
            if html_url:
                return str(html_url)

        val = event.metadata.get("url") or event.metadata.get("html_url")
        return str(val) if val else None
    except Exception:
        # Prevent any link parsing errors from breaking status reporting
        return None


@dataclass
class Candidate:
    record: dict[str, Any]
    symbols: list[str] = field(default_factory=list)
    readable: bool = False
    sources: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return str(self.record["id"])

    @property
    def blocking(self) -> bool:
        return bool(self.record.get("severity") == Severity.HARD_BLOCK.value)

    @property
    def severity(self) -> str:
        return "blocking" if self.blocking else "advisory"

    @property
    def text(self) -> str:
        return redaction.serve(self.record.get("summary"), self.record.get("summary_redacted"))


def own_records(tenant_id: UUID, repo_name: str, pr_num: int | None, head_ref: str) -> Any:
    """Records whose base event or a linked event belongs to this pull request."""
    records, links, ev = CanonicalContext.__table__, ContextEventLink.__table__, events_table.c
    meta = ev.code_metadata
    belongs = [meta["pull_request_id"].astext == str(pr_num)] if pr_num is not None else []
    if head_ref:
        belongs.append(meta["branch"].astext == head_ref)
    if not belongs:
        return records.c.id.is_(None)
    pr_events = select(ev.event_id).where(
        (ev.tenant_id == tenant_id)
        & (ev.source_platform == SourcePlatform.GITHUB.value)
        & (meta["repository"].astext == repo_name)
        & or_(*belongs)
    )
    linked = select(links.c.canonical_id).where(
        (links.c.tenant_id == tenant_id) & links.c.event_id.in_(pr_events)
    )
    return records.c.base_event_id.in_(pr_events) | records.c.id.in_(linked)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}(s)"


class PRStatusWorker:
    """Judges each pull request against the recorded decisions, then posts its commit status
    and one comment."""

    def __init__(
        self,
        redis_client: Redis,
        db_engine: AsyncEngine | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.redis = redis_client
        self.db_engine = db_engine
        self._llm = llm_client
        self.settings = get_settings()
        self.batch_size = self.settings.redis_stream_batch_size
        self.poll_timeout = self.settings.redis_stream_poll_timeout_ms
        self._fallback_task: asyncio.Task | None = None
        self._main_task: asyncio.Task | None = None

    async def _ensure_group(self, topic: str, group: str) -> None:
        try:
            await self.redis.xgroup_create(name=topic, groupname=group, id="0", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" in str(e):
                pass
            else:
                raise

    async def start(self) -> None:
        self._fallback_task = asyncio.create_task(self._poll_fallback_tasks())

        topic = TOPIC
        group = "pr_status_reporter_group"
        consumer_name = "pr_status_reporter_consumer"
        await self._ensure_group(topic, group)
        logger.info("pr_status.started", topic=topic, group=group)

        next_reclaim = 0.0
        while True:
            try:
                now = time.monotonic()
                if now >= next_reclaim:
                    next_reclaim = now + self.settings.redis_stream_reclaim_interval_ms / 1000
                    reclaimed = await pending.reclaim(
                        self.redis,
                        topic,
                        group,
                        consumer_name,
                        min_idle_ms=self.settings.redis_stream_reclaim_min_idle_ms,
                        count=self.batch_size,
                    )
                    for msg_id, raw_payload in reclaimed:
                        await self._process_message(topic, group, msg_id, raw_payload)
                batch = pending.entries(
                    await self.redis.xreadgroup(
                        groupname=group,
                        consumername=consumer_name,
                        streams={topic: ">"},
                        count=self.batch_size,
                        block=self.poll_timeout,
                    )
                )
                for msg_id, raw_payload in batch:
                    await self._process_message(topic, group, msg_id, raw_payload)

            except asyncio.CancelledError:
                logger.info("pr_status.cancelled")
                if self._fallback_task:
                    self._fallback_task.cancel()
                break
            except Exception as e:
                logger.exception("pr_status.loop_error", error=str(e))
                await asyncio.sleep(2)

    async def _poll_fallback_tasks(self) -> None:
        """Periodic loop to check and process expired items in the fallback ZSET."""
        logger.info("pr_status.fallback_poller_started")
        while True:
            try:
                now = time.time()
                # Fetch all expired items (score <= now)
                expired = await self.redis.zrangebyscore("ci.pr.fallback_zset", "-inf", now)
                if expired:
                    for item_bytes in expired:
                        item_str = (
                            item_bytes.decode("utf-8")
                            if isinstance(item_bytes, bytes)
                            else str(item_bytes)
                        )
                        # Another API process may hold this poller too; ZREM decides who handles it.
                        if not await self.redis.zrem("ci.pr.fallback_zset", item_str):
                            continue
                        try:
                            data = json.loads(item_str)
                            sha = data.get("sha")

                            status = await self.redis.get(f"ci:pr_status:{sha}")
                            status_str = (
                                status.decode("utf-8") if isinstance(status, bytes) else status
                            )

                            if status_str != "completed":
                                repo_name = f"{data.get('owner')}/{data.get('repo')}"
                                tenant_id = UUID(data["tenant_id"])
                                logger.warning(
                                    "pr_check.timed_out",
                                    repo=repo_name,
                                    sha=sha,
                                    pr=data.get("pr_number"),
                                    status=status_str,
                                )
                                state = await post_not_evaluated(
                                    self.redis, self.db_engine, tenant_id, repo_name, sha
                                )
                                await self._record_check(
                                    tenant_id,
                                    repo_name,
                                    int(data.get("pr_number") or 0),
                                    state,
                                    self.settings.pr_check_mode,
                                    [],
                                    {},
                                )
                        except Exception as e:
                            logger.exception("failed_to_process_fallback_item", error=str(e))

                await self._requeue_due_retries()
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                logger.info("pr_status.fallback_poller_cancelled")
                break
            except Exception as e:
                logger.exception("pr_status.fallback_poller_error", error=str(e))
                await asyncio.sleep(1.0)

    async def _requeue_due_retries(self) -> None:
        for item in await self.redis.zrangebyscore(RETRY_ZSET, "-inf", time.time()):
            if await self.redis.zrem(RETRY_ZSET, item):
                text = item.decode("utf-8") if isinstance(item, bytes) else str(item)
                await self.redis.xadd(TOPIC, fields={"payload": text})

    def _model(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(
                task_models={
                    LLMTask.PR_CHECK: build_chat_model(
                        self.settings.pr_check_model,
                        purpose=LLMTask.PR_CHECK.value,
                        settings=self.settings,
                    )
                }
            )
        return self._llm

    async def _judge(self, candidate: Candidate, diff: str) -> tuple[str, str]:
        try:
            return await self._model().judge_change(candidate.text, diff)
        except Exception as e:
            logger.warning("pr_check.judge_failed", record_id=candidate.id, error=str(e)[:300])
            return FAILED, ""

    async def _candidates(
        self,
        tenant_id: UUID,
        repo_name: str,
        affected: list[AffectedContext],
        mode: str,
        pr_num: int | None = None,
        head_ref: str = "",
    ) -> tuple[list[Candidate], int]:
        """Live decision or blocking records attached to changed symbols, then (when judging)
        live blocking records in scope for the repository, newest first; also how many in-scope
        ones were left out. Records made from or joined by this pull request's own events are
        never candidates."""
        assert self.db_engine is not None
        records = CanonicalContext.__table__
        symbols: dict[str, list[str]] = {}
        for drift in affected:
            for cid in drift.canonical_ids:
                names = symbols.setdefault(str(cid), [])
                if drift.symbol_name not in names:
                    names.append(drift.symbol_name)
        found: dict[str, Candidate] = {}
        left_out = 0
        async with tenant_context(self.db_engine, tenant_id) as conn:
            if symbols:
                rows = await conn.execute(
                    select(records).where(
                        (records.c.tenant_id == tenant_id)
                        & records.c.id.in_([UUID(c) for c in symbols])
                        & live_records(records)
                        & decisions_or_blocking(records)
                        & ~own_records(tenant_id, repo_name, pr_num, head_ref)
                    )
                )
                for row in rows.mappings():
                    found[str(row["id"])] = Candidate(dict(row), symbols[str(row["id"])])
            if mode == "judge":
                rows = await conn.execute(
                    select(records)
                    .where(
                        (records.c.tenant_id == tenant_id)
                        & live_records(records)
                        & (records.c.severity == Severity.HARD_BLOCK.value)
                        & _scope_filter(tenant_id, repo_identifier=repo_name)
                        & ~own_records(tenant_id, repo_name, pr_num, head_ref)
                    )
                    .order_by(records.c.created_at.desc(), records.c.id)
                    .limit(SCOPE_CANDIDATES + 1)
                )
                in_scope = [dict(r) for r in rows.mappings()]
                if len(in_scope) > SCOPE_CANDIDATES:
                    in_scope = in_scope[:SCOPE_CANDIDATES]
                    left_out = await self._count_in_scope(tenant_id, repo_name) - SCOPE_CANDIDATES
                    logger.warning("pr_check.scope_capped", repo=repo_name, left_out=left_out)
                for row in in_scope:
                    found.setdefault(str(row["id"]), Candidate(row))
        return list(found.values()), left_out

    async def _count_in_scope(self, tenant_id: UUID, repo_name: str) -> int:
        assert self.db_engine is not None
        records = CanonicalContext.__table__
        async with tenant_context(self.db_engine, tenant_id) as conn:
            return int(
                await conn.scalar(
                    select(func.count())
                    .select_from(records)
                    .where(
                        (records.c.tenant_id == tenant_id)
                        & live_records(records)
                        & (records.c.severity == Severity.HARD_BLOCK.value)
                        & _scope_filter(tenant_id, repo_identifier=repo_name)
                    )
                )
                or 0
            )

    async def _describe(
        self, tenant_id: UUID, candidates: list[Candidate], audience: RepoAudience
    ) -> None:
        """Mark what every reader of the repository may see and fetch its source links, latest
        update and pending replacement note."""
        assert self.db_engine is not None
        for c in candidates:
            c.readable = audience.allows(c.record.get("access_control"))
        shown = [c for c in candidates if c.readable]
        if not shown:
            return
        async with tenant_context(self.db_engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            for c in shown:
                try:
                    event_ids = await store.get_events_for_context(tenant_id, UUID(c.id))
                    events = (
                        await store.get_events_by_ids(tenant_id, event_ids) if event_ids else []
                    )
                except Exception as e:
                    logger.error("pr_check.sources_failed", record_id=c.id, error=str(e))
                    continue
                for ev in events:
                    if not audience.allows(ev.access_control.model_dump(mode="json")):
                        continue
                    url = _build_source_link(ev)
                    if url:
                        name = str(getattr(ev.source_platform, "value", ev.source_platform))
                        label = _PLATFORM_NAMES.get(name.lower(), f"{name.capitalize()} event")
                        c.sources.append(f"[{label}]({url})")
            try:
                await record_notes.annotate(
                    conn, tenant_id, [c.record for c in shown], limit=1, allows=audience.allows
                )
            except Exception as e:
                logger.error("pr_check.notes_failed", error=str(e))

    def _node_url(self, repo_name: str, cid_str: str) -> str:
        return build_node_url(self.settings.web_base_url, repo_name, f"context:{cid_str}")

    def _restricted_line(self, repo_name: str, c: Candidate) -> str:
        return (
            f"- Restricted record (severity: `{c.record.get('severity') or 'unknown'}`). "
            "Its content is visible in Checkcord to people with access: "
            f"[View in Checkcord]({self._node_url(repo_name, c.id)})"
        )

    def _record_lines(self, repo_name: str, c: Candidate, reason: str | None) -> list[str]:
        text = _one_line(c.text)[:COMMENT_TEXT_CHARS] or "No description provided."
        lines = [f"- **{c.severity.capitalize()}**: {text}"]
        if c.symbols:
            lines.append("  - Attached to: " + ", ".join(f"`{s}`" for s in c.symbols))
        if reason:
            lines.append(f"  - Reason: {_one_line(reason)}")
        links = [*c.sources, f"[View in Checkcord]({self._node_url(repo_name, c.id)})"]
        lines.append("  - Sources: " + " · ".join(links))
        for update in c.record.get("updates", [])[:1]:
            lines.append(
                f"  - Latest update ({_day(update['happened_at'])}): {_one_line(update['text'])}"
            )
        note = c.record.get("may_be_replaced_by")
        if note:
            lines.append(
                f"  - A newer record from {_day(note['created_at'])} may replace this; "
                f"an admin has not confirmed it yet: {_one_line(note['summary'])}"
            )
        return lines

    def _section(
        self,
        repo_name: str,
        title: str,
        items: list[Candidate],
        verdicts: dict[str, tuple[str, str]],
    ) -> list[str]:
        if not items:
            return []
        items = sorted(items, key=lambda c: not c.blocking)
        lines = [f"#### {title}", ""]
        for c in (c for c in items if c.readable):
            lines += self._record_lines(repo_name, c, verdicts.get(c.id, ("", ""))[1])
        restricted = [c for c in items if not c.readable]
        if restricted:
            lines += ["", "#### Restricted records", ""]
            lines += [self._restricted_line(repo_name, c) for c in restricted]
        return [*lines, ""]

    def _comment(
        self,
        repo_name: str,
        sha: str,
        candidates: list[Candidate],
        verdicts: dict[str, tuple[str, str]],
        mode: str,
        unjudged: list[Candidate],
        left_out: int,
    ) -> str | None:
        lines: list[str] = []
        if mode == "touch":
            lines += self._section(
                repo_name, "Records attached to code this pull request changes", candidates, {}
            )
        else:
            conflicts = [c for c in candidates if verdicts[c.id][0] == CONFLICTS]
            lines += self._section(repo_name, "Conflicts", conflicts, verdicts)
            lines += self._section(repo_name, "Could not evaluate", unjudged, {})
            attached = [
                c
                for c in candidates
                if c.symbols and c.readable and verdicts[c.id][0] in (COMPLIES, UNRELATED)
            ]
            if attached:
                lines += ["#### Attached to code you changed", ""]
                for c in sorted(attached, key=lambda c: not c.blocking)[:ATTACHED_SHOWN]:
                    text = _one_line(c.text)[:ATTACHED_TEXT_CHARS]
                    names = ", ".join(f"`{s}`" for s in c.symbols)
                    lines.append(
                        f"- {names}: {text} ({c.severity}, "
                        f"[View in Checkcord]({self._node_url(repo_name, c.id)}))"
                    )
                if len(attached) > ATTACHED_SHOWN:
                    lines.append(f"- and {len(attached) - ATTACHED_SHOWN} more")
                lines.append("")
        if not lines and not left_out:
            return None
        last = f"Checked at {sha[:7]}."
        if left_out:
            last += (
                f" {left_out} more blocking decision(s) in scope were not checked; only the "
                f"newest {SCOPE_CANDIDATES} were."
            )
        return "\n".join([COMMENT_MARKER, "### Checkcord", "", *lines, "---", last])

    async def _write_comment(
        self, client: GitHubRestClient, repo_name: str, pr_num: int, sha: str, body: str | None
    ) -> None:
        owner, repo = repo_name.split("/", 1)
        try:
            comments = await client.list_issue_comments(owner, repo, pr_num)
        except Exception as e:
            logger.warning("pr_check.comments_unlisted", repo=repo_name, pr=pr_num, error=str(e))
            return
        ours = [c for c in comments if str(c.get("body") or "").startswith(COMMENT_MARKER)]
        if body is None:
            if not ours:
                return
            body = f"{COMMENT_MARKER}\nNo conflicts with recorded decisions at {sha[:7]}."
        if ours and await client.update_issue_comment(owner, repo, int(ours[-1]["id"]), body):
            return
        await client.create_issue_comment(owner=owner, repo=repo, issue_number=pr_num, body=body)

    async def _record_check(
        self,
        tenant_id: UUID,
        repo_name: str,
        pr_num: int,
        state: str,
        mode: str,
        candidates: list[Candidate],
        verdicts: dict[str, tuple[str, str]],
        skipped: bool = False,
    ) -> None:
        """One audit entry per posted check, with the verdict on every record judged."""
        if self.db_engine is None:
            return
        shown = [c for c in candidates if c.readable]
        flagged = [c for c in candidates if verdicts[c.id][0] in (CONFLICTS, ATTACHED)]
        try:
            async with tenant_context(self.db_engine, tenant_id) as conn:
                await audit.record(
                    conn,
                    tenant_id,
                    audit.Actor(channel=audit.API),
                    audit.PR_CHECK,
                    params={
                        "repo": repo_name,
                        "pr": pr_num,
                        "state": state,
                        "mode": mode,
                        "flagged": len(flagged),
                        "conflicts": sum(v[0] == CONFLICTS for v in verdicts.values()),
                        "blocked": state == "failure",
                        "error": state == "error",
                        "skipped": skipped,
                        "verdicts": {cid: v[0] for cid, v in verdicts.items()},
                    },
                    record_ids=sorted(c.id for c in shown),
                    redacted=sum(c.text != (c.record.get("summary") or "") for c in shown),
                    result_count=len(shown),
                )
        except Exception as e:
            logger.warning("pr_check_audit_failed", repo=repo_name, error=str(e))

    def _outcome(
        self,
        mode: str,
        candidates: list[Candidate],
        verdicts: dict[str, tuple[str, str]],
    ) -> tuple[str, str]:
        flagged = CONFLICTS if mode == "judge" else ATTACHED
        hits = [c for c in candidates if verdicts[c.id][0] == flagged]
        blocking = [c for c in hits if c.blocking]
        failed = [c for c in candidates if c.blocking and verdicts[c.id][0] == FAILED]
        if blocking:
            if mode == "touch":
                return "failure", (
                    f"{_plural(len(blocking), 'blocking decision')} attached to changed code. "
                    "See the Checkcord comment."
                )
            return "failure", (
                f"Conflicts with {_plural(len(blocking), 'blocking decision')}. "
                "See the Checkcord comment."
            )
        if failed:
            return "error", (
                f"Could not evaluate {_plural(len(failed), 'blocking decision')}. "
                "Push again or re-deliver the webhook to retry."
            )
        if hits:
            return "success", f"{_plural(len(hits), 'advisory note')}. See the Checkcord comment."
        return "success", "No conflicts with recorded decisions."

    async def _process_message(
        self, topic: str, group: str, msg_id: str, raw_payload: dict[str, str]
    ) -> None:
        try:
            payload_str = raw_payload.get("payload")
            if not payload_str:
                logger.error("missing_payload_field_in_message", payload=raw_payload)
                await self.redis.xack(topic, group, msg_id)
                return
            try:
                payload = PRContextCheckedPayload.model_validate_json(payload_str)
            except ValidationError as e:
                logger.error("invalid_context_checked_payload", error=str(e))
                await self.redis.xack(topic, group, msg_id)
                return
            try:
                await self._check(payload)
            except Exception as err:
                logger.exception("pr_check.failed", attempt=payload.attempt, error=str(err)[:300])
                if payload.attempt < JUDGE_ATTEMPTS:
                    await self._retry(payload, payload.verdicts)
                else:
                    await write_dead_letter(self.redis, TOPIC, raw_payload, traceback.format_exc())
                    await self._give_up(payload)
            await self.redis.xack(topic, group, msg_id)
        except Exception as err:
            logger.exception("failed_to_process_checked_status_message", error=str(err))
            try:
                await write_dead_letter(self.redis, TOPIC, raw_payload, traceback.format_exc())
                await self.redis.xack(topic, group, msg_id)
            except Exception as dlq_err:
                logger.error("failed_to_route_to_dlq", error=str(dlq_err))

    async def _retry(
        self, payload: PRContextCheckedPayload, verdicts: dict[str, tuple[str, str]]
    ) -> None:
        retry = payload.model_copy(update={"attempt": payload.attempt + 1, "verdicts": verdicts})
        await self.redis.zadd(RETRY_ZSET, {retry.model_dump_json(): time.time() + RETRY_SECONDS})
        logger.warning(
            "pr_check.retry_scheduled",
            repo=payload.repository_full_name,
            pr=payload.pull_request_number,
            sha=payload.head_sha,
            attempt=payload.attempt + 1,
        )
        owner, repo_short = payload.repository_full_name.split("/", 1)
        try:
            client = GitHubRestClient(
                await github_token_for(
                    self.db_engine, payload.tenant_id, payload.repository_full_name
                )
            )
            await client.create_commit_status(
                owner=owner,
                repo=repo_short,
                sha=payload.head_sha,
                state="pending",
                description=f"Could not evaluate yet; retrying in {RETRY_SECONDS} s "
                f"(attempt {payload.attempt + 1} of {JUDGE_ATTEMPTS}).",
            )
        except Exception as e:
            logger.warning("pr_check.pending_post_failed", error=str(e)[:300])

    async def _give_up(self, payload: PRContextCheckedPayload) -> None:
        try:
            state = await post_not_evaluated(
                self.redis,
                self.db_engine,
                payload.tenant_id,
                payload.repository_full_name,
                payload.head_sha,
            )
        except Exception as e:
            logger.error("pr_check.error_post_failed", sha=payload.head_sha, error=str(e)[:300])
            return
        await self._record_check(
            payload.tenant_id,
            payload.repository_full_name,
            payload.pull_request_number,
            state,
            self.settings.pr_check_mode,
            [],
            {},
        )

    async def _check(self, payload: PRContextCheckedPayload) -> None:
        repo_name, pr_num, sha = (
            payload.repository_full_name,
            payload.pull_request_number,
            payload.head_sha,
        )
        tenant_id = payload.tenant_id
        owner, repo_short = repo_name.split("/", 1)
        await self.redis.set(f"ci:pr_status:{sha}", "checking")
        head_key = f"ci:pr_head:{tenant_id}:{repo_name}#{pr_num}"
        if payload.attempt == 1:
            await self.redis.set(head_key, sha, ex=7 * 24 * 3600)
        client = GitHubRestClient(await github_token_for(self.db_engine, tenant_id, repo_name))
        mode = self.settings.pr_check_mode

        async def post(state: str, description: str) -> None:
            await client.create_commit_status(
                owner=owner, repo=repo_short, sha=sha, state=state, description=description
            )

        if payload.head_ref.startswith(OWN_BRANCH_PREFIX):
            await post("success", "Checkcord's own update; not checked.")
            await self.redis.set(f"ci:pr_status:{sha}", "completed")
            await self._record_check(
                tenant_id, repo_name, pr_num, "success", mode, [], {}, skipped=True
            )
            return
        if self.db_engine is None:
            await post(*not_evaluated())
            await self.redis.set(f"ci:pr_status:{sha}", "completed")
            return

        candidates, left_out = await self._candidates(
            tenant_id, repo_name, payload.affected_contexts, mode, pr_num, payload.head_ref
        )
        verdicts = dict(payload.verdicts)
        if mode == "judge":
            usage.set_tenant(tenant_id)
            for c in candidates:
                if verdicts.get(c.id, (FAILED, ""))[0] == FAILED:
                    verdicts[c.id] = await self._judge(c, payload.diff)
            await usage.flush(self.db_engine)
        else:
            verdicts = {c.id: (ATTACHED, "") for c in candidates}
        verdicts = {c.id: verdicts[c.id] for c in candidates}

        state, description = self._outcome(mode, candidates, verdicts)
        if state == "error" and payload.attempt < JUDGE_ATTEMPTS:
            await self._retry(payload, verdicts)
            return
        if state == "error" and self.settings.pr_check_on_error == "success":
            state, description = not_evaluated()

        audience = (
            await resolve_repo_audience(self.db_engine, tenant_id, repo_name, client)
            if candidates
            else RepoAudience()
        )
        await self._describe(tenant_id, candidates, audience)
        unjudged = [c for c in candidates if c.blocking and verdicts[c.id][0] == FAILED]
        body = self._comment(
            repo_name,
            sha,
            candidates,
            verdicts,
            mode,
            unjudged if state == "error" else [],
            left_out,
        )
        latest = await self.redis.get(head_key)
        latest = latest.decode("utf-8") if isinstance(latest, bytes) else latest
        if latest in (None, sha):
            await self._write_comment(client, repo_name, pr_num, sha, body)
        await post(state, description)
        await self.redis.set(f"ci:pr_status:{sha}", "completed")
        await self._record_check(tenant_id, repo_name, pr_num, state, mode, candidates, verdicts)
        logger.info(
            "pr_check.posted",
            repo=repo_name,
            pr=pr_num,
            sha=sha,
            state=state,
            judged=sum(v[0] != ATTACHED for v in verdicts.values()),
        )

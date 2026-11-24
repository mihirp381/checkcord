import asyncio
import re
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from checkcord.bus.interface import EventBus
from checkcord.core import metrics
from checkcord.core.audience import join_refusal
from checkcord.core.exceptions import LinkageAdjudicationError
from checkcord.core.scoring import calculate_confidence_score
from checkcord.llm.client import LLMClient
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline.redaction import redact_for_storage
from checkcord.services import code_links, records
from checkcord.storage import audit
from checkcord.storage.models import CanonicalContext
from checkcord.storage.postgres import (
    PostgresEventStore,
    event_audience,
    events_table,
    tenant_context,
)

logger = structlog.get_logger(__name__)

CONTEXTS_CREATED = "contexts.created"


class HoldChangedError(Exception):
    """The event or its matched record was released or held while it was being linked."""


def context_created_event(
    tenant_id: UUID,
    context_id: UUID,
    summary: str,
    author: AuthorInfo,
    access_control: AccessControl,
) -> CanonicalEvent:
    return CanonicalEvent(
        tenant_id=tenant_id,
        source_platform=SourcePlatform.CUSTOM,
        source_type=SourceType.CUSTOM_PAYLOAD,
        source_id=str(context_id),
        happened_at=datetime.now(UTC),
        author=author,
        content_raw=summary,
        content_clean=summary,
        access_control=access_control,
        metadata={"canonical_context_id": str(context_id)},
    )


def extract_identifiers(text: str) -> set[str]:
    """Extract PR numbers and Jira/ticket-like identifiers from text.
    Examples:
        - PR-105, PR#105, #105
        - JIRA-123, PROJ-456, ABC-789
    """
    if not text:
        return set()
    # Normalize text to uppercase
    text = text.upper()

    # Matches JIRA-like projects: letters (2 or more) followed by a hyphen and digits
    # e.g. JIRA-123, PROJ-456
    jira_pattern = re.compile(r"\b[A-Z]{2,}-\d+\b")
    jira_matches = jira_pattern.findall(text)

    # Matches PR indicators: PR-123, PR#123, #123
    pr_pattern = re.compile(r"\bPR[-#]?\d+\b|#\d+\b")
    pr_matches = pr_pattern.findall(text)

    clean_ids = set()
    for m in jira_matches:
        clean_ids.add(m)
    for m in pr_matches:
        # Standardize PR identifiers to 'PR-<number>' or '#<number>'
        digits = re.search(r"\d+", m)
        if digits:
            clean_ids.add(f"PR-{digits.group(0)}")

    return clean_ids


_CODE_NAME = re.compile(
    r"\b(?:[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b"
)


def closest(event: CanonicalEvent, matches: list[dict[str, Any]]) -> dict[str, Any]:
    """The nearest match that names the same code as the event, else the nearest match."""
    names = set(_CODE_NAME.findall(event.content_clean))
    for match in matches:
        if names & set(_CODE_NAME.findall(match.get("summary") or "")):
            return match
    return matches[0]


EXCERPT_CHARS = 600
RESTATEMENT_SIMILARITY = 0.97
_PUNCTUATION = re.compile(r"[^\w\s]")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def _normalised(text: str) -> str:
    return " ".join(_PUNCTUATION.sub("", text.casefold()).split())


def is_restatement(text: str, summary: str, similarity: float | None) -> bool:
    if similarity is not None and similarity >= RESTATEMENT_SIMILARITY:
        return True
    return _normalised(text) == _normalised(summary)


def cut_excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """At most ``limit`` characters, cut back to a sentence end or a word boundary."""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    ends = [m.end() for m in _SENTENCE_END.finditer(head)]
    if ends and ends[-1] >= limit // 2:
        head = head[: ends[-1]]
    elif not text[limit].isspace() and " " in head:
        head = head[: head.rindex(" ")]
    return head.rstrip() + "…"


def update_excerpt(text: str, match: dict[str, Any]) -> str | None:
    """The text to keep for an event joining ``match``; None when it only restates it."""
    if is_restatement(text, match.get("summary") or "", match.get("similarity")):
        return None
    return cut_excerpt(text) or None


async def _store_score(store: PostgresEventStore, tenant_id: UUID, record_id: UUID) -> None:
    events = await store.get_events_by_ids(
        tenant_id, await store.get_events_for_context(tenant_id, record_id)
    )
    await store.update_context_scores_batch_global(
        [(tenant_id, record_id, calculate_confidence_score(events))]
    )


async def _audiences(
    conn: AsyncConnection, tenant_id: UUID, event_id: UUID, record_ids: list[UUID]
) -> tuple[dict[str, Any], dict[UUID, dict[str, Any]]]:
    """The stored audience of the event and of each candidate record."""
    row = (
        (
            await conn.execute(
                select(events_table.c.access_control, events_table.c.access_allowed_ids).where(
                    (events_table.c.tenant_id == tenant_id) & (events_table.c.event_id == event_id)
                )
            )
        )
        .mappings()
        .first()
    )
    records_ = CanonicalContext.__table__
    found = await conn.execute(
        select(records_.c.id, records_.c.access_control).where(
            (records_.c.tenant_id == tenant_id) & records_.c.id.in_(record_ids)
        )
    )
    return event_audience(row or {}), {r.id: dict(r.access_control or {}) for r in found}


def joinable(
    event_ac: dict[str, Any], record_acs: dict[UUID, dict[str, Any]], matches: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The candidates every reader of the event can read; the others are counted."""
    kept = []
    for match in matches:
        refusal = join_refusal(event_ac, record_acs.get(UUID(match["context_id"]), {}))
        if refusal is None:
            kept.append(match)
        else:
            metrics.LINKAGE_SKIPPED.labels(refusal).inc()
    return kept


class LinkageWorker:
    """Links each event to a matching record or starts a new record."""

    def __init__(
        self,
        bus: EventBus,
        engine: AsyncEngine,
        llm_client: LLMClient,
        group_name: str = "linkage_workers",
        consumer_name: str = "linkage-worker-1",
        llm_timeout_seconds: float = 60.0,
    ) -> None:
        self.bus = bus
        self.engine = engine
        self.llm_client = llm_client
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.llm_timeout_seconds = llm_timeout_seconds
        self.stop_event = asyncio.Event()

    async def process_message(self, event: CanonicalEvent) -> None:
        """Processes an event, runs the linkage checks, and updates context store."""
        log = logger.bind(
            tenant_id=str(event.tenant_id),
            event_id=str(event.event_id),
        )

        log.info("linkage.processing_event")

        async with tenant_context(self.engine, event.tenant_id, include_held=True) as conn:
            store = PostgresEventStore(conn)
            stored, held = await store.get_event_hold(event.tenant_id, event.event_id)
            matches = event.metadata.get("clustering_matches", [])
            candidate_ids = [UUID(m["context_id"]) for m in matches if m.get("context_id")]
            holds = await store.get_record_holds(event.tenant_id, candidate_ids)
            event_ac, record_acs = await _audiences(
                conn, event.tenant_id, event.event_id, candidate_ids
            )
        if not stored:
            log.warning("linkage.skipping_event_not_stored")
            return
        matches = [
            m
            for m in matches
            if m.get("context_id") and holds.get(UUID(m["context_id"]), "") == held
        ]
        # A record shows its updates only to their own readers, so an event may join only a
        # record every one of its readers can read; the others start their own record.
        matches = joinable(event_ac, record_acs, matches)
        matched: dict[str, Any] | None = None
        changed: dict[str, Any] | None = None
        adjudication_failed = False
        reason = ""
        # Merged into the earlier record, a replacing decision would be served under its summary.
        stated = bool(event.metadata.get("states_replacement")) or records.states_replacement(
            event.content_clean
        )

        for match in [] if stated else matches:
            context_id = match.get("context_id")
            summary = match.get("summary", "")

            if not context_id:
                continue

            ids_summary = extract_identifiers(summary)
            ids_event = extract_identifiers(event.content_clean)

            if ids_summary and ids_event:
                if not ids_summary.isdisjoint(ids_event):
                    log.info("linkage.deterministic_match_found", context_id=context_id)
                    matched = match
                    break
                log.info("linkage.deterministic_mismatch_skipping", context_id=context_id)
                continue

            # A timeout or error is an unknown verdict, never "different".
            try:
                verdict = await asyncio.wait_for(
                    self.llm_client.judge_linkage(summary, event.content_clean),
                    timeout=self.llm_timeout_seconds,
                )
            except TimeoutError:
                log.warning("linkage.llm_timeout", context_id=context_id)
                adjudication_failed = True
                reason = (
                    f"the linkage model gave no verdict within {self.llm_timeout_seconds:g}s "
                    "(LLM_TIMEOUT_SECONDS)"
                )
                continue
            except Exception as e:
                log.exception("linkage.llm_error", context_id=context_id, error=str(e))
                adjudication_failed = True
                reason = f"the linkage model call failed: {e}"
                continue
            log.info("linkage.llm_verdict", context_id=context_id, verdict=verdict)
            if verdict == "same":
                matched = match
                break
            if verdict == "changes":
                changed = match
                break

        if matched is None and changed is None and adjudication_failed:
            # Starting a new record on an unknown verdict would mint a duplicate; the bus
            # retries the event and finally dead-letters it.
            log.warning(
                "linkage.adjudication_failed_deferring_to_retry",
                candidate_count=len(matches),
            )
            raise LinkageAdjudicationError(
                f"Linkage adjudication failed for event {event.event_id}: {reason}; "
                "no deterministic or LLM match found among the other candidates."
            )

        # The model passes run before the write transaction, never inside it.
        matched_context_id = matched["context_id"] if matched else None
        stored_redaction = None
        excerpt = None
        if matched is None:
            stored_redaction = await redact_for_storage(event.content_clean)
        else:
            text = update_excerpt(event.content_clean, matched)
            excerpt = await redact_for_storage(text) if text else None
        replaces = None
        if matched is None and held is None and matches:
            if stated:
                replaces = await self._replacement(event, closest(event, matches), "stated")
            elif changed is not None:
                replaces = await self._replacement(event, changed, "model")

        try:
            async with tenant_context(self.engine, event.tenant_id, include_held=True) as conn:
                store = PostgresEventStore(conn)
                if (await store.get_event_hold(event.tenant_id, event.event_id))[1] != held:
                    raise HoldChangedError(str(event.event_id))

                if matched_context_id:
                    ctx_uuid = UUID(matched_context_id)
                    found = await store.get_record_holds(event.tenant_id, [ctx_uuid])
                    if found.get(ctx_uuid, "") != held:
                        raise HoldChangedError(matched_context_id)
                    await store.link_context_to_event(
                        tenant_id=event.tenant_id,
                        canonical_id=ctx_uuid,
                        event_id=event.event_id,
                        excerpt=excerpt,
                    )
                    if excerpt is not None:
                        await audit.instructions_removed(
                            conn, event.tenant_id, ctx_uuid, excerpt.text
                        )
                    if excerpt is not None and held is None:
                        await code_links.link_record(conn, event.tenant_id, ctx_uuid, excerpt.text)
                    await _store_score(store, event.tenant_id, ctx_uuid)
                    log.info(
                        "linkage.event_linked_to_existing_context",
                        context_id=matched_context_id,
                        excerpt=excerpt is not None,
                    )
                else:
                    embedding = event.metadata.get("embedding")
                    # context_type is set only here, from the originating event's
                    # metadata, and never changed on a later merge.
                    new_context_id = await store.save_canonical_context(
                        tenant_id=event.tenant_id,
                        summary=event.content_clean,
                        confidence_score=0.0,
                        embedding=embedding,
                        access_control=event.access_control.model_dump(mode="python"),
                        scope_repo_identifier=(
                            event.code_metadata.repository if event.code_metadata else None
                        ),
                        context_type=event.metadata.get("context_type"),
                        base_event_id=event.event_id,
                        held_connection=held,
                        redaction=stored_redaction,
                    )
                    await store.link_context_to_event(
                        tenant_id=event.tenant_id,
                        canonical_id=new_context_id,
                        event_id=event.event_id,
                    )
                    if stored_redaction is not None:
                        await audit.instructions_removed(
                            conn, event.tenant_id, new_context_id, stored_redaction.text
                        )
                    await _store_score(store, event.tenant_id, new_context_id)
                    log.info(
                        "linkage.new_canonical_context_bootstrapped", context_id=str(new_context_id)
                    )
                    if replaces is not None:
                        await records.suggest(
                            conn,
                            event.tenant_id,
                            UUID(replaces[0]["context_id"]),
                            new_context_id,
                            replaces[1],
                            replaces[0].get("similarity"),
                        )
                        log.info(
                            "linkage.replacement_suggested",
                            record_id=replaces[0]["context_id"],
                            basis=replaces[1],
                        )

                    # A held record gets no code links or context files until it is released.
                    if held is None:
                        context_event = context_created_event(
                            event.tenant_id,
                            new_context_id,
                            event.content_clean,
                            event.author,
                            event.access_control,
                        )
                        await self.bus.publish(CONTEXTS_CREATED, context_event)
                        log.info(
                            "linkage.published_contexts_created_event",
                            context_id=str(new_context_id),
                        )

        except Exception as e:
            log.exception("linkage.db_operation_failed", error=str(e))
            raise

    async def _replacement(
        self, event: CanonicalEvent, target: dict[str, Any], basis: Literal["stated", "model"]
    ) -> tuple[dict[str, Any], Literal["stated", "model"]] | None:
        """The target and basis when the new record may replace it: same scope, live record,
        and for a model verdict a compatible type."""
        target_id = UUID(target["context_id"])
        async with tenant_context(self.engine, event.tenant_id) as conn:
            store = PostgresEventStore(conn)
            row = await store.get_canonical_context(event.tenant_id, target_id)
            if row is None or row.get("retired_at") or row.get("held_connection"):
                return None
            if not await self._same_scope(store, event, target_id):
                return None
        if basis == "model" and not records.replaceable_by(
            row.get("context_type"), event.metadata.get("context_type")
        ):
            return None
        return target, basis

    @staticmethod
    async def _same_scope(store: PostgresEventStore, event: CanonicalEvent, other: UUID) -> bool:
        repo = event.code_metadata.repository if event.code_metadata else None
        rows = await store.get_context_scope_rows(event.tenant_id, other)
        return rows == ([{"scope_type": "repo", "scope_value": repo}] if repo else [])

    async def _subscribe_to_topic(self, topic: str) -> None:
        """Background task running the Redis subscription loop."""
        try:
            await self.bus.subscribe(
                topic=topic,
                group=self.group_name,
                consumer_name=self.consumer_name,
                handler=self.process_message,
            )
        except asyncio.CancelledError:
            logger.info("linkage.subscription_cancelled", topic=topic)
        except Exception as e:
            logger.exception("linkage.subscription_crashed", topic=topic, error=str(e))

    async def start(self, topics: list[str]) -> None:
        """Starts the subscription and blocks until stopped."""
        logger.info("linkage.starting", topics=topics, group=self.group_name)

        tasks = [asyncio.create_task(self._subscribe_to_topic(topic)) for topic in topics]

        await self.stop_event.wait()
        logger.info("linkage.shutdown_initiated")

        for t in tasks:
            t.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("linkage.shutdown_complete")

    def stop(self) -> None:
        """Triggers graceful shutdown."""
        self.stop_event.set()

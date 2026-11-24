"""Turns due discussions into outcome events on ``events.raw.custom``."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import openai
import structlog
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from checkcord.bus.interface import EventBus
from checkcord.core.telemetry import increment_counter
from checkcord.llm.client import LLMClient
from checkcord.llm.outcomes import Outcome, UnusableReply
from checkcord.models.common import AccessControl, AuthorInfo, ChatMetadata
from checkcord.models.enums import AccessVisibility, AclType, SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.storage import redis_trees

logger = structlog.get_logger(__name__)

CONTEXT_TYPES = {
    "decision": "adr",
    "constraint": "constraint",
    "requirement": "business_requirement",
    "risk": "risk",
}

_TRANSPORT = (
    openai.APIConnectionError,
    openai.RateLimitError,
    openai.InternalServerError,
    httpx.TransportError,
    TimeoutError,
    ConnectionError,
    RedisConnectionError,
    RedisTimeoutError,
)


def is_transport_error(error: BaseException) -> bool:
    if isinstance(error, _TRANSPORT):
        return True
    status = getattr(error, "status_code", None)
    if status is None and isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
    return isinstance(status, int) and (status == 429 or status >= 500)


def build_outcome_event(
    tenant_id: UUID, tree: dict[str, str], outcome: Outcome, n: int
) -> CanonicalEvent:
    platform = SourcePlatform(tree["platform"])
    channel_id = tree["channel_id"]
    messages: list[dict[str, Any]] = json.loads(tree.get("messages") or "[]")
    cited = [messages[i - 1] for i in outcome.messages if 0 < i <= len(messages)] or messages
    if tree.get("access_control"):
        access_control = AccessControl.model_validate_json(tree["access_control"])
    else:
        access_control = AccessControl(
            is_public=False,
            visibility=AccessVisibility.PRIVATE,
            acl_type=AclType.CHANNEL_MEMBERS,
            source_group_id=channel_id,
        )
    latest = max(cited, key=lambda m: m.get("happened_at") or "", default={})
    author = (
        AuthorInfo(
            source_id=latest["author_id"],
            email=latest.get("author_email"),
            display_name=latest.get("author_name"),
        )
        if latest.get("author_id")
        else AuthorInfo(source_id="unknown")
    )
    times = sorted(m["happened_at"] for m in cited if m.get("happened_at"))
    thread_ids = list(dict.fromkeys(m["thread_ts"] for m in cited if m.get("thread_ts")))
    content = "\n".join([outcome.statement, *(f"- {d}" for d in outcome.details)])
    return CanonicalEvent(
        tenant_id=tenant_id,
        source_platform=platform,
        source_type=SourceType.MESSAGE,
        source_id=f"chat-tree:{tree['tree_id']}:{n}",
        happened_at=datetime.fromisoformat(times[-1]) if times else datetime.now(UTC),
        author=author,
        content_raw=content,
        content_clean=content,
        source_raw_payload=outcome.to_json(),
        access_control=access_control,
        audience_container=channel_id,
        connection=tree.get("connection") or None,
        chat_metadata=ChatMetadata(
            channel_id=channel_id,
            thread_id=thread_ids[0] if thread_ids else None,
            reply_count=max(len(cited) - 1, 0),
        ),
        metadata={
            "context_type": CONTEXT_TYPES[outcome.kind],
            "domain_tag": tree.get("domain_tag") or "HYBRID",
            "states_replacement": outcome.replaces_earlier,
            "chat_source": {
                "platform": platform.value,
                "channel_id": channel_id,
                "channel_name": tree.get("channel_name") or None,
                "message_ids": [m["message_id"] for m in cited if m.get("message_id")],
                "discussion_message_ids": [
                    m["message_id"] for m in messages if m.get("message_id")
                ],
                "thread_ids": thread_ids,
                "authors": list(dict.fromkeys(m["author_id"] for m in cited if m.get("author_id"))),
                "first_message_at": times[0] if times else None,
                "last_message_at": times[-1] if times else None,
                "url": next((m["url"] for m in cited if m.get("url")), None),
            },
        },
    )


class ChatExtractor:
    """Claims due discussions, asks what they settled and publishes one event per outcome."""

    def __init__(
        self,
        bus: EventBus,
        redis: Redis,
        llm_client: LLMClient,
        poll_interval_seconds: float = 5.0,
    ) -> None:
        self.bus = bus
        self.redis = redis
        self.llm_client = llm_client
        self.poll_interval_seconds = poll_interval_seconds
        self.stop_event = asyncio.Event()

    async def poll_due_once(self) -> int:
        done = 0
        for tenant_id, tree_id in await redis_trees.due_discussions(self.redis):
            tree = await redis_trees.claim_discussion(self.redis, tenant_id, tree_id)
            if tree is None:
                continue
            await self._extract(tenant_id, tree_id, tree)
            done += 1
        return done

    async def _fail(self, tenant_id: UUID, tree_id: str, error: BaseException) -> None:
        unusable = not is_transport_error(error)
        parked = await redis_trees.fail_discussion(
            self.redis, tenant_id, tree_id, f"{type(error).__name__}: {error}", unusable=unusable
        )
        kind = "unusable" if unusable else "transport"
        increment_counter("chat_extraction_failed_total", tags={"error": kind})
        logger.warning(
            "chat_extractor.failed",
            tenant_id=str(tenant_id),
            tree_id=tree_id,
            error=str(error)[:300],
            kind=kind,
            parked=parked,
        )

    async def _extract(self, tenant_id: UUID, tree_id: str, tree: dict[str, str]) -> None:
        messages: list[dict[str, Any]] = json.loads(tree.get("messages") or "[]")
        version = tree.get("version", "")
        done_ids = [m["message_id"] for m in messages if m.get("message_id")]
        if not messages or not tree.get("platform") or not tree.get("channel_id"):
            await redis_trees.finish_discussion(self.redis, tenant_id, tree_id, version, done_ids)
            return
        items = redis_trees.cached_outcomes(tree)
        if items is None:
            try:
                found = await self.llm_client.extract_outcomes(messages)
            except Exception as e:
                if not isinstance(e, UnusableReply | ValueError):
                    logger.exception("chat_extractor.model_error", error=str(e))
                await self._fail(tenant_id, tree_id, e)
                return
            items = [o.to_json() for o in found]
            await redis_trees.cache_outcomes(self.redis, tenant_id, tree_id, version, items)
        try:
            items = await self._settled(tenant_id, tree["platform"], messages, items)
        except Exception as e:
            logger.exception("chat_extractor.settled_error", error=str(e))
            await self._fail(tenant_id, tree_id, e)
            return
        if not items:
            increment_counter(
                "chat_discussion_no_outcome_total", tags={"platform": tree["platform"]}
            )
            await redis_trees.no_outcome(self.redis, tenant_id)
        try:
            for n, item in enumerate(items, 1):
                event = build_outcome_event(tenant_id, tree, Outcome.from_json(item), n)
                await self.bus.publish("events.raw.custom", event)
        except Exception as e:
            await self._fail(tenant_id, tree_id, e)
            return
        increment_counter("chat_extraction_published_total", tags={"platform": tree["platform"]})
        await redis_trees.finish_discussion(self.redis, tenant_id, tree_id, version, done_ids)
        logger.info(
            "chat_extractor.published",
            tenant_id=str(tenant_id),
            tree_id=tree_id,
            outcomes=len(items),
        )

    async def _settled(
        self, tenant_id: UUID, platform: str, messages: list[dict[str, Any]], items: list[Any]
    ) -> list[Any]:
        if not items:
            return items
        discussion = self.llm_client.discussion_text(messages)
        kept = [
            item
            for item in items
            if await self.llm_client.judge_settled(discussion, item["statement"]) == "SETTLED"
        ]
        dropped = len(items) - len(kept)
        for _ in range(dropped):
            increment_counter("chat_outcome_not_settled_total", tags={"platform": platform})
        if dropped:
            await redis_trees.not_settled(self.redis, tenant_id, dropped)
        return kept

    async def _poller_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await asyncio.sleep(self.poll_interval_seconds)
                await self.poll_due_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("chat_extractor.poll_error", error=str(e))

    async def start(self) -> None:
        logger.info("chat_extractor.starting")
        task = asyncio.create_task(self._poller_loop())
        await self.stop_event.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def stop(self) -> None:
        self.stop_event.set()

"""Chat messages become records whose audience is the channel's, end to end."""

import json
import zlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app_role_helpers import app_database_url, create_app_role, drop_app_role
from fastapi.testclient import TestClient
from key_helpers import KeySpec, seeded_keys
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.adapters.chat.providers.slack.audience import resolve_slack_audience
from checkcord.core.config import get_settings
from checkcord.llm.outcomes import Outcome
from checkcord.main import app
from checkcord.models.common import AccessControl
from checkcord.models.enums import SourcePlatform
from checkcord.models.event import CanonicalEvent
from checkcord.models.raw_payloads import RawChatPayload
from checkcord.pipeline.chat_extractor import ChatExtractor
from checkcord.pipeline.chat_router import ChatRouter
from checkcord.pipeline.clustering import ClusteringWorker
from checkcord.pipeline.consumer import PipelineConsumer
from checkcord.pipeline.enricher import PipelineEnricher
from checkcord.pipeline.linkage import LinkageWorker
from checkcord.pipeline.scrubber import PipelineScrubber
from checkcord.services import purge
from checkcord.storage import audit, redis_trees
from checkcord.storage.postgres import PostgresEventStore, create_checkcord_engine, tenant_context
from checkcord.storage.redis_trees import ACTIVE_TREES_ZSET, get_active_discourse_trees

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

SLACK = SourcePlatform.SLACK
REPO = "acme/chat-records"
START = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
ADMIN_URL = get_settings().database_url


class CaptureBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    async def publish(self, topic: str, event: Any) -> str:
        self.published.append((topic, event))
        return "0-0"

    def take(self, topic: str) -> list[Any]:
        taken = [e for t, e in self.published if t == topic]
        self.published = [(t, e) for t, e in self.published if t != topic]
        return taken


class FakeModel:
    """Every block goes to one tree id; a discussion settles one requirement made of its
    messages' text."""

    def __init__(self) -> None:
        self.shared_tree_id = str(uuid4())
        self.down = False

    async def classify_chat_message(self, text: str) -> bool:
        return True

    async def assign_discourse_tree(
        self, active_trees: list[dict[str, Any]], message_block: list[dict[str, Any]]
    ) -> dict[str, str]:
        return {"tree_id": self.shared_tree_id, "domain_tag": "PROBLEM_SPACE", "summary": "s"}

    async def extract_outcomes(self, messages: list[dict[str, Any]]) -> list[Outcome]:
        if self.down:
            raise ConnectionError("model down")
        statement = " | ".join(m["content"] for m in messages)
        return [Outcome("requirement", statement, (), (), False)]

    async def judge_linkage(self, summary: str, content: str) -> str:
        return "same"

    def discussion_text(self, messages: list[dict[str, Any]]) -> str:
        return "\n".join(m["content"] for m in messages)

    async def judge_settled(self, discussion_text: str, statement: str) -> str:
        return "SETTLED"


class FixedEmbedder:
    async def embed_text(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 1023


class SpreadEmbedder:
    async def embed_text(self, text: str) -> list[float]:
        vector = [0.0] * 1024
        vector[zlib.crc32(text.encode()) % 1024] = 1.0
        return vector


class SlackClient:
    def __init__(self, is_private: bool, members: list[str] | None) -> None:
        self.is_private = is_private
        self.members = members

    async def conversations_info(self, channel: str) -> dict[str, Any]:
        return {"ok": True, "channel": {"is_private": self.is_private}}

    async def conversations_members(self, **kwargs: Any) -> dict[str, Any]:
        if self.members is None:
            return {"ok": False, "error": "missing_scope"}
        return {"ok": True, "members": self.members}


@dataclass
class World:
    tenant_id: UUID
    engine: AsyncEngine
    redis: Redis
    members: dict[str, UUID]


async def _admin(sql: str, params: dict[str, Any]) -> list[Any]:
    engine = create_async_engine(ADMIN_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            result = await conn.execute(text(sql), params)
            return list(result.fetchall()) if result.returns_rows else []
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def world(async_redis_client: Redis) -> AsyncGenerator[World, None]:
    tenant_id = uuid4()
    specs = [
        KeySpec("chat_records_member", tenant_id, user_email=f"m-{tenant_id}@acme.example"),
        KeySpec("chat_records_other", tenant_id, user_email=f"o-{tenant_id}@acme.example"),
        KeySpec("chat_records_unbound", tenant_id),
    ]
    async with seeded_keys(specs) as users:
        member = users[f"m-{tenant_id}@acme.example"]
        other = users[f"o-{tenant_id}@acme.example"]
        for native_id, internal_id in (
            ("U_MEMBER", member),
            ("U_OTHER", other),
            ("U_GHOST", None),
        ):
            await _admin(
                "INSERT INTO chat_users (tenant_id, platform, native_id, display_name, "
                "internal_id, metadata) VALUES (:t, 'slack', :n, :n, :i, '{}')",
                {"t": tenant_id, "n": native_id, "i": internal_id},
            )
        role, url = await create_app_role()
        engine = create_checkcord_engine(url)
        try:
            with app_database_url(url):
                yield World(
                    tenant_id, engine, async_redis_client, {"member": member, "other": other}
                )
        finally:
            await engine.dispose()
            await drop_app_role(role)
            for table in (
                "context_event_links",
                "canonical_contexts",
                "events",
                "chat_users",
                "audience_containers",
            ):
                await _admin(f"DELETE FROM {table} WHERE tenant_id = :t", {"t": tenant_id})


def _payload(
    w: World,
    channel_id: str,
    n: int,
    audience: AccessControl,
    *,
    author_id: str = "U_MEMBER",
    text: str | None = None,
    subtype: str | None = None,
) -> RawChatPayload:
    content = text or f"PROJ-42 payment retries message {n}"
    return RawChatPayload(
        tenant_id=w.tenant_id,
        platform=SLACK,
        channel_id=channel_id,
        channel_name=f"chan-{channel_id}",
        author_id=author_id,
        author_name=author_id.lower(),
        message_id=f"1700000000.00000{n}",
        thread_ts="1700000000.000001",
        happened_at=START + timedelta(minutes=n),
        content_raw=content,
        content_clean=content,
        access_control=audience,
        raw_event={"subtype": subtype} if subtype else {},
    )


async def _audience(w: World, channel_id: str, client: SlackClient) -> AccessControl:
    return await resolve_slack_audience(
        client=client,
        redis=None,
        db_engine=w.engine,
        tenant_id=w.tenant_id,
        channel_id=channel_id,
    )


async def _discuss(
    w: World, model: FakeModel, bus: CaptureBus, payloads: list[RawChatPayload]
) -> list[CanonicalEvent]:
    """Run payloads from events.raw.chat to the extracted events on events.raw.custom."""
    scrubber = PipelineScrubber(bus=bus, llm_client=model)
    router = ChatRouter(bus=bus, redis=w.redis, llm_client=model, engine=w.engine)
    for p in payloads:
        await scrubber.process_message(json.loads(p.model_dump_json()))
    for p in bus.take("events.scrubbed.chat"):
        await router.process_message(p)
    for channel_id in dict.fromkeys(p.channel_id for p in payloads):
        await router.flush_buffer_block(w.tenant_id, SLACK, channel_id)
    return await _extract(w, model, bus)


async def _extract(w: World, model: FakeModel, bus: CaptureBus) -> list[CanonicalEvent]:
    for member in await w.redis.zrange(ACTIVE_TREES_ZSET, 0, -1):
        found = redis_trees.parse_member(member)
        if found is not None:
            await w.redis.delete(f"discussion_lease:{found[0]}:{found[1]}")
        await w.redis.zadd(ACTIVE_TREES_ZSET, {member: 0})
    extractor = ChatExtractor(bus=bus, redis=w.redis, llm_client=model)
    await extractor.poll_due_once()
    return list(bus.take("events.raw.custom"))


async def _store(
    w: World, model: FakeModel, events: list[CanonicalEvent], embedder: Any = None
) -> list[UUID]:
    """Run extracted events through enrichment, storage, clustering and linkage."""
    bus = CaptureBus()
    enricher = PipelineEnricher(bus=bus, engine=w.engine)
    consumer = PipelineConsumer(bus, w.engine, embedder=embedder or FixedEmbedder())
    clustering = ClusteringWorker(bus=bus, engine=w.engine)
    linkage = LinkageWorker(bus=bus, engine=w.engine, llm_client=model)
    for event in events:
        await enricher.process_message(event)
        [enriched] = bus.take("events.enriched.chat")
        await consumer.process_message(enriched)
        for pending in bus.take("events.clustering.pending"):
            await clustering.process_message(pending)
        for linking in bus.take("events.linkage.pending"):
            await linkage.process_message(linking)
    rows = await _admin(
        "SELECT DISTINCT canonical_id FROM context_event_links WHERE tenant_id = :t",
        {"t": w.tenant_id},
    )
    return [r[0] for r in rows]


def _status(token: str, context_id: UUID) -> tuple[int, dict[str, Any]]:
    with TestClient(app) as client:
        res = client.get(
            f"/api/v1/graph/node/context:{context_id}?repo_identifier={REPO}",
            headers={"Authorization": f"Bearer {token}"},
        )
    return res.status_code, res.json()


async def _stored_audience(context_id: UUID) -> dict[str, Any]:
    [row] = await _admin(
        "SELECT access_control, context_type FROM canonical_contexts WHERE id = :id",
        {"id": context_id},
    )
    return {**row[0], "context_type": row[1]}


async def test_private_channel_record_is_readable_by_members_only(world: World) -> None:
    model = FakeModel()
    audience = await _audience(world, "G_PRIV", SlackClient(True, ["U_MEMBER", "U_GHOST"]))
    assert audience.is_public is False
    assert audience.allowed_identity_ids == [world.members["member"]]

    events = await _discuss(
        world, model, CaptureBus(), [_payload(world, "G_PRIV", i, audience) for i in range(3)]
    )
    [event] = events
    assert event.metadata["chat_source"]["channel_id"] == "G_PRIV"
    assert len(event.metadata["chat_source"]["message_ids"]) == 3
    [context_id] = await _store(world, model, events)

    stored = await _stored_audience(context_id)
    assert stored["is_public"] is False
    assert stored["allowed_identity_ids"] == [str(world.members["member"])]
    assert stored["context_type"] == "business_requirement"

    status, body = _status("chat_records_member", context_id)
    assert status == 200
    assert "G_PRIV" in json.dumps(body)
    assert _status("chat_records_other", context_id)[0] == 404
    assert _status("chat_records_unbound", context_id)[0] == 404

    with TestClient(app) as client:
        for token, code in (("chat_records_member", 200), ("chat_records_other", 404)):
            res = client.get(
                f"/api/v1/graph/context/{context_id}/scope/history",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert res.status_code == code
        unknown = uuid4()
        res = client.get(
            f"/api/v1/graph/context/{unknown}/scope/history",
            headers={"Authorization": "Bearer chat_records_other"},
        )
        assert (res.status_code, res.json()["detail"]) == (
            404,
            f"Context {unknown} not found or access denied",
        )


async def test_public_channel_record_is_public(world: World) -> None:
    model = FakeModel()
    audience = await _audience(world, "C_PUB", SlackClient(False, None))
    assert audience.is_public is True

    events = await _discuss(world, model, CaptureBus(), [_payload(world, "C_PUB", 1, audience)])
    [context_id] = await _store(world, model, events)

    assert (await _stored_audience(context_id))["is_public"] is True
    assert _status("chat_records_other", context_id)[0] == 200
    assert _status("chat_records_unbound", context_id)[0] == 200


async def test_two_channels_never_share_a_tree(world: World) -> None:
    model = FakeModel()
    public = await _audience(world, "C_ONE", SlackClient(False, None))
    payloads = [_payload(world, "C_ONE", 1, public), _payload(world, "C_TWO", 2, public)]
    bus = CaptureBus()
    scrubber = PipelineScrubber(bus=bus, llm_client=model)
    router = ChatRouter(bus=bus, redis=world.redis, llm_client=model)
    for p in payloads:
        await scrubber.process_message(p)
        [scrubbed] = bus.take("events.scrubbed.chat")
        await router.process_message(scrubbed)
        await router.flush_buffer_block(world.tenant_id, SLACK, p.channel_id)

    [one] = await get_active_discourse_trees(world.redis, world.tenant_id, SLACK, "C_ONE")
    [two] = await get_active_discourse_trees(world.redis, world.tenant_id, SLACK, "C_TWO")
    assert one["tree_id"] != two["tree_id"]
    assert one["message_count"] == two["message_count"] == "1"


async def test_public_restatement_of_a_private_decision_is_its_own_record(world: World) -> None:
    model = FakeModel()
    private = await _audience(world, "G_MIX", SlackClient(True, ["U_MEMBER"]))
    public = await _audience(world, "C_MIX", SlackClient(False, None))

    events = await _discuss(
        world,
        model,
        CaptureBus(),
        [_payload(world, "G_MIX", 1, private), _payload(world, "C_MIX", 2, public)],
    )
    assert len(events) == 2
    events.sort(key=lambda e: e.access_control.is_public)
    records = await _store(world, model, events)
    assert len(records) == 2
    by_public = {(await _stored_audience(r))["is_public"]: r for r in records}

    restricted = await _stored_audience(by_public[False])
    assert restricted["allowed_identity_ids"] == [str(world.members["member"])]
    assert _status("chat_records_member", by_public[False])[0] == 200
    assert _status("chat_records_other", by_public[False])[0] == 404
    for token in ("chat_records_member", "chat_records_other", "chat_records_unbound"):
        assert _status(token, by_public[True])[0] == 200


async def test_unresolvable_membership_gives_empty_audience(world: World) -> None:
    model = FakeModel()
    audience = await _audience(world, "G_LOCKED", SlackClient(True, None))
    assert audience.is_public is False
    assert audience.allowed_identity_ids == []

    events = await _discuss(world, model, CaptureBus(), [_payload(world, "G_LOCKED", 1, audience)])
    [context_id] = await _store(world, model, events)

    stored = await _stored_audience(context_id)
    assert stored["is_public"] is False
    assert stored["allowed_identity_ids"] == []
    assert _status("chat_records_member", context_id)[0] == 404


async def test_admin_grant_recomputes_record_audience(world: World) -> None:
    model = FakeModel()
    audience = await _audience(world, "G_GRANT", SlackClient(True, None))
    events = await _discuss(world, model, CaptureBus(), [_payload(world, "G_GRANT", 1, audience)])
    [context_id] = await _store(world, model, events)
    member = world.members["member"]

    async with tenant_context(world.engine, world.tenant_id) as conn:
        store = PostgresEventStore(conn)
        assert await store.update_admin_overrides(world.tenant_id, events[0].event_id, [member])
        assert await store.recompute_access_for_event(world.tenant_id, events[0].event_id) == 1

    assert (await _stored_audience(context_id))["admin_overrides"] == [str(member)]
    assert _status("chat_records_member", context_id)[0] == 200
    assert _status("chat_records_other", context_id)[0] == 404


async def _summaries(w: World) -> list[str]:
    rows = await _admin(
        "SELECT summary FROM canonical_contexts WHERE tenant_id = :t", {"t": w.tenant_id}
    )
    return [r[0] for r in rows]


async def _outcome_events(w: World) -> list[str]:
    rows = await _admin(
        "SELECT source_id FROM events WHERE tenant_id = :t AND source_id LIKE 'chat-tree:%'",
        {"t": w.tenant_id},
    )
    return [r[0] for r in rows]


async def _change(w: World, model: FakeModel, payload: RawChatPayload) -> list[UUID]:
    bus = CaptureBus()
    scrubber = PipelineScrubber(bus=bus, llm_client=model)
    router = ChatRouter(bus=bus, redis=w.redis, llm_client=model, engine=w.engine)
    await scrubber.process_message(json.loads(payload.model_dump_json()))
    for p in bus.take("events.scrubbed.chat"):
        await router.process_message(p)
    return await _store(w, model, await _extract(w, model, bus))


async def _audit_names(w: World) -> list[str]:
    rows = await _admin(
        "SELECT action FROM audit_log_entries WHERE tenant_id = :t ORDER BY created_at",
        {"t": w.tenant_id},
    )
    return [r[0] for r in rows]


async def test_model_down_then_back_gives_one_record_per_discussion(world: World) -> None:
    model = FakeModel()
    public = await _audience(world, "C_A", SlackClient(False, None))
    model.down = True
    bus = CaptureBus()
    payloads = [
        _payload(world, channel, 1, public, text=f"PROJ-42 rule for {channel}")
        for channel in ("C_A", "C_B", "C_C")
    ]
    assert await _discuss(world, model, bus, payloads) == []
    assert (await redis_trees.discussion_counts(world.redis, world.tenant_id))["waiting"] == 3

    model.down = False
    events = await _extract(world, model, bus)
    assert len(events) == 3
    records = await _store(world, model, events, SpreadEmbedder())
    assert len(records) == 3
    assert await _extract(world, model, bus) == []
    assert len(await _outcome_events(world)) == 3


async def test_a_crash_between_publish_and_delete_stores_one_record(world: World) -> None:
    model = FakeModel()
    public = await _audience(world, "C_CRASH", SlackClient(False, None))
    bus = CaptureBus()
    with (
        patch.object(redis_trees, "finish_discussion", side_effect=RuntimeError("killed")),
        pytest.raises(RuntimeError),
    ):
        await _discuss(world, model, bus, [_payload(world, "C_CRASH", 1, public)])
    first = bus.take("events.raw.custom")
    again = await _extract(world, model, bus)
    assert [e.source_id for e in first] == [e.source_id for e in again]

    records = await _store(world, model, first + again)
    assert len(records) == 1
    assert len(await _outcome_events(world)) == 1
    assert (await redis_trees.discussion_counts(world.redis, world.tenant_id))["waiting"] == 0


async def test_an_edit_changes_the_record_text(world: World) -> None:
    model = FakeModel()
    public = await _audience(world, "C_EDIT", SlackClient(False, None))
    events = await _discuss(
        world, model, CaptureBus(), [_payload(world, "C_EDIT", n, public) for n in (1, 2)]
    )
    await _store(world, model, events)
    [before] = await _summaries(world)
    assert "message 2" in before

    edited = _payload(
        world, "C_EDIT", 2, public, text="PROJ-42 retries five times", subtype="message_changed"
    )
    [record] = await _change(world, model, edited)

    [after] = await _summaries(world)
    assert "retries five times" in after and "message 2" not in after
    assert "message 1" in after
    assert "source.message_changed" in await _audit_names(world)
    assert _status("chat_records_other", record)[0] == 200


async def test_deleting_the_only_message_removes_the_record(world: World) -> None:
    model = FakeModel()
    public = await _audience(world, "C_DEL1", SlackClient(False, None))
    events = await _discuss(world, model, CaptureBus(), [_payload(world, "C_DEL1", 1, public)])
    assert len(await _store(world, model, events)) == 1

    gone = _payload(world, "C_DEL1", 1, public, subtype="message_deleted")
    assert await _change(world, model, gone) == []

    assert await _summaries(world) == []
    assert await _outcome_events(world) == []
    assert "source.message_deleted" in await _audit_names(world)


async def test_deleting_one_of_three_messages_rebuilds_from_two(world: World) -> None:
    model = FakeModel()
    public = await _audience(world, "C_DEL3", SlackClient(False, None))
    payloads = [_payload(world, "C_DEL3", n, public) for n in (1, 2, 3)]
    await _store(world, model, await _discuss(world, model, CaptureBus(), payloads))

    gone = _payload(world, "C_DEL3", 2, public, subtype="message_deleted")
    [record] = await _change(world, model, gone)

    [summary] = await _summaries(world)
    assert "message 1" in summary and "message 3" in summary
    assert "message 2" not in summary
    [outcome] = await _outcome_events(world)
    assert not outcome.startswith(f"chat-tree:{model.shared_tree_id}")


async def test_deleting_a_person_leaves_nothing_they_wrote(world: World) -> None:
    model = FakeModel()
    public = await _audience(world, "C_TEAM", SlackClient(False, None))
    payloads = [
        _payload(world, "C_TEAM", 1, public, text="PROJ-42 keep three retries"),
        _payload(world, "C_TEAM", 2, public, author_id="U_OTHER", text="PROJ-42 zebra quota"),
        _payload(world, "C_TEAM", 3, public, text="PROJ-42 agreed"),
    ]
    await _store(world, model, await _discuss(world, model, CaptureBus(), payloads))
    assert any("zebra" in s for s in await _summaries(world))

    deleted = await purge.delete_person(
        world.engine,
        world.tenant_id,
        actor=audit.Actor(channel=audit.API),
        user_id=world.members["other"],
        redis=world.redis,
    )
    assert deleted is not None
    await _store(world, model, await _extract(world, model, CaptureBus()))

    [summary] = await _summaries(world)
    assert "keep three retries" in summary and "agreed" in summary
    rows = await _admin(
        "SELECT content_raw, content_clean, source_raw_payload::text FROM events "
        "WHERE tenant_id = :t",
        {"t": world.tenant_id},
    )
    assert rows
    assert not any("zebra" in (value or "") for row in rows for value in row)
    redacted = await _admin(
        "SELECT summary_redacted FROM canonical_contexts WHERE tenant_id = :t",
        {"t": world.tenant_id},
    )
    assert not any("zebra" in (r[0] or "") for r in redacted)

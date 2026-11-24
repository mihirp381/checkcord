"""Tests for context-level access_control aggregation.

* the record takes its base event's audience (the first linked event by default)
* admin_overrides = union across linked events
"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text

from checkcord.core.config import get_settings
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.storage.postgres import (
    PostgresEventStore,
    aggregate_access_control,
    create_checkcord_engine,
    tenant_context,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


# ---------------------------------------------------------------------------
# Pure unit tests -- no database required
# ---------------------------------------------------------------------------


def test_aggregate_empty_list_returns_fail_closed_default() -> None:
    result = aggregate_access_control([])
    assert result["is_public"] is False
    assert result["allowed_identity_ids"] == []
    assert result["admin_overrides"] == []
    assert result["visibility"] == "private"


def test_aggregate_single_public_event() -> None:
    result = aggregate_access_control(
        [{"access_control": {"is_public": True}, "access_allowed_ids": []}]
    )
    assert result["is_public"] is True
    assert result["allowed_identity_ids"] == []
    assert result["visibility"] == "public"


def test_aggregate_single_private_event_keeps_its_allowed_ids() -> None:
    u1, u2 = str(uuid4()), str(uuid4())
    result = aggregate_access_control(
        [{"access_control": {"is_public": False}, "access_allowed_ids": [u1, u2]}]
    )
    assert result["is_public"] is False
    assert sorted(result["allowed_identity_ids"]) == sorted([u1, u2])
    assert result["visibility"] == "restricted"


def test_aggregate_two_private_events_takes_the_first() -> None:
    u1, u2, u3 = str(uuid4()), str(uuid4()), str(uuid4())
    result = aggregate_access_control(
        [
            {"access_control": {"is_public": False}, "access_allowed_ids": [u1, u2]},
            {"access_control": {"is_public": False}, "access_allowed_ids": [u2, u3]},
        ]
    )
    assert sorted(result["allowed_identity_ids"]) == sorted([u1, u2])


def test_aggregate_takes_the_named_base_event() -> None:
    u1, u2 = str(uuid4()), str(uuid4())
    first, second = uuid4(), uuid4()
    result = aggregate_access_control(
        [
            {"event_id": first, "access_control": {"is_public": True}, "access_allowed_ids": []},
            {
                "event_id": second,
                "access_control": {"is_public": False},
                "access_allowed_ids": [u1, u2],
            },
        ],
        base_event_id=second,
    )
    assert result["is_public"] is False
    assert sorted(result["allowed_identity_ids"]) == sorted([u1, u2])


def test_aggregate_public_base_stays_public_with_a_private_follow_up() -> None:
    u1 = str(uuid4())
    result = aggregate_access_control(
        [
            {"access_control": {"is_public": True}, "access_allowed_ids": []},
            {"access_control": {"is_public": False}, "access_allowed_ids": [u1]},
        ]
    )
    assert result["is_public"] is True
    assert result["allowed_identity_ids"] == []


def test_aggregate_private_source_keeps_record_restricted() -> None:
    u1 = str(uuid4())
    result = aggregate_access_control(
        [
            {"access_control": {"is_public": False}, "access_allowed_ids": [u1]},
            {"access_control": {"is_public": True}, "access_allowed_ids": []},
        ]
    )
    assert result["is_public"] is False
    assert result["allowed_identity_ids"] == [u1]


def test_aggregate_admin_overrides_are_unioned() -> None:
    a1, a2 = str(uuid4()), str(uuid4())
    result = aggregate_access_control(
        [
            {
                "access_control": {"is_public": False, "admin_overrides": [a1]},
                "access_allowed_ids": [],
            },
            {
                "access_control": {"is_public": False, "admin_overrides": [a2]},
                "access_allowed_ids": [],
            },
        ]
    )
    assert sorted(result["admin_overrides"]) == sorted([a1, a2])


def test_aggregate_handles_mixed_uuid_and_string_inputs() -> None:
    """DB rows may return UUID objects (native array) or strings (JSONB) -- both must work."""
    u1 = uuid4()
    result = aggregate_access_control(
        [{"access_control": {"is_public": False}, "access_allowed_ids": [u1]}]
    )
    assert result["allowed_identity_ids"] == [str(u1)]


# ---------------------------------------------------------------------------
# Integration tests -- real Postgres, exercising save_canonical_context /
# link_context_to_event end-to-end.
# ---------------------------------------------------------------------------


def _make_event(tenant_id: object, access_control: dict, content: str) -> CanonicalEvent:
    return CanonicalEvent(
        tenant_id=tenant_id,
        source_platform=SourcePlatform.SLACK,
        source_type=SourceType.MESSAGE,
        source_id=f"msg_{uuid4().hex[:8]}",
        happened_at=datetime.now(UTC),
        author={"source_id": "user_test"},
        content_raw=content,
        content_clean=content,
        access_control=access_control,
    )


async def test_link_context_to_event_keeps_the_first_events_audience() -> None:
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    user_a, user_b = uuid4(), uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)

            event_1 = _make_event(
                tenant_id,
                {"is_public": False, "allowed_identity_ids": [str(user_a), str(user_b)]},
                "first source",
            )
            await store.save_event(event_1)

            context_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="bootstrap summary",
                confidence_score=0.0,
                access_control=event_1.access_control.model_dump(mode="python"),
            )
            await store.link_context_to_event(tenant_id, context_id, event_1.event_id)

            ctx = await store.get_canonical_context(tenant_id, context_id)
            assert ctx is not None
            assert sorted(ctx["access_control"]["allowed_identity_ids"]) == sorted(
                [str(user_a), str(user_b)]
            )

            event_2 = _make_event(
                tenant_id,
                {"is_public": False, "allowed_identity_ids": [str(user_a)]},
                "second source",
            )
            await store.save_event(event_2)
            await store.link_context_to_event(tenant_id, context_id, event_2.event_id)

            ctx_after = await store.get_canonical_context(tenant_id, context_id)
            assert ctx_after is not None
            assert sorted(ctx_after["access_control"]["allowed_identity_ids"]) == sorted(
                [str(user_a), str(user_b)]
            )
            assert ctx_after["access_control"]["is_public"] is False
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_event_links"))
            await conn.execute(text("DELETE FROM canonical_contexts"))
            await conn.execute(
                text("DELETE FROM events WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_link_context_to_event_private_follow_up_keeps_public_record_public() -> None:
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)

            public_event = _make_event(tenant_id, {"is_public": True}, "public source")
            await store.save_event(public_event)

            context_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="bootstrap summary",
                confidence_score=0.0,
                access_control=public_event.access_control.model_dump(mode="python"),
            )
            await store.link_context_to_event(tenant_id, context_id, public_event.event_id)

            member = str(uuid4())
            private_event = _make_event(
                tenant_id, {"is_public": False, "allowed_identity_ids": [member]}, "private source"
            )
            await store.save_event(private_event)
            await store.link_context_to_event(tenant_id, context_id, private_event.event_id)

            ctx = await store.get_canonical_context(tenant_id, context_id)
            assert ctx is not None
            assert ctx["access_control"]["is_public"] is True
            assert ctx["access_control"]["allowed_identity_ids"] == []
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_event_links"))
            await conn.execute(text("DELETE FROM canonical_contexts"))
            await conn.execute(
                text("DELETE FROM events WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()


async def test_save_canonical_context_defaults_to_fail_closed_when_omitted() -> None:
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()

    try:
        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            context_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="no access_control passed",
                confidence_score=0.0,
            )
            ctx = await store.get_canonical_context(tenant_id, context_id)
            assert ctx is not None
            assert ctx["access_control"]["is_public"] is False
            assert ctx["access_control"]["allowed_identity_ids"] == []
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM canonical_contexts"))
        await engine.dispose()


async def test_concurrent_links_to_same_context_both_reflected_in_aggregate() -> None:
    """The FOR UPDATE row lock in link_context_to_event.

    Two events matched to the same pre-existing context at nearly the same
    time (two consumer instances, two in-flight messages) must both end up
    reflected in the final aggregate -- without the lock, whichever recompute
    commits last can silently overwrite the other's contribution because its
    read of "all linked events" happened before it could see the other's
    just-inserted link.
    """
    settings = get_settings()
    engine = create_checkcord_engine(settings.database_url)
    tenant_id = uuid4()
    u1, u2, u3 = uuid4(), uuid4(), uuid4()
    a1, a2 = str(uuid4()), str(uuid4())

    try:
        bootstrap_event = _make_event(
            tenant_id,
            {"is_public": False, "allowed_identity_ids": [str(u1), str(u2), str(u3)]},
            "bootstrap source",
        )
        event_b = _make_event(
            tenant_id,
            {"is_public": False, "allowed_identity_ids": [str(u1)], "admin_overrides": [a1]},
            "race B",
        )
        event_c = _make_event(
            tenant_id,
            {"is_public": False, "allowed_identity_ids": [str(u3)], "admin_overrides": [a2]},
            "race C",
        )

        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            await store.save_event(bootstrap_event)
            await store.save_event(event_b)
            await store.save_event(event_c)
            context_id = await store.save_canonical_context(
                tenant_id=tenant_id,
                summary="concurrency race bootstrap",
                confidence_score=0.0,
                access_control=bootstrap_event.access_control.model_dump(mode="python"),
            )
            await store.link_context_to_event(tenant_id, context_id, bootstrap_event.event_id)

        async def _link(event_id: object) -> None:
            async with tenant_context(engine, tenant_id) as race_conn:
                race_store = PostgresEventStore(race_conn)
                await race_store.link_context_to_event(tenant_id, context_id, event_id)

        # Two separate connections, two separate transactions, racing to
        # recompute the same context's access_control concurrently.
        await asyncio.gather(_link(event_b.event_id), _link(event_c.event_id))

        async with tenant_context(engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            ctx = await store.get_canonical_context(tenant_id, context_id)
            assert ctx is not None
            # Both overrides must be present; a missing one means a concurrent link was lost.
            assert sorted(ctx["access_control"]["admin_overrides"]) == sorted([a1, a2])
            assert sorted(ctx["access_control"]["allowed_identity_ids"]) == sorted(
                str(u) for u in (u1, u2, u3)
            )
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM context_event_links"))
            await conn.execute(text("DELETE FROM canonical_contexts"))
            await conn.execute(
                text("DELETE FROM events WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        await engine.dispose()

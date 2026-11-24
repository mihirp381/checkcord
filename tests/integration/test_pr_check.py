"""Which records a pull request is judged against, read from the database."""

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app_role_helpers import app_database_url, create_app_role, drop_app_role
from pr_check_fakes import FakeJudge, FakePullRequest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.core.config import get_settings
from checkcord.storage import audit
from checkcord.storage.postgres import create_checkcord_engine
from checkcord.workers.pr_status_reporter import SCOPE_CANDIDATES, PRStatusWorker

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ADMIN_URL = get_settings().database_url
REPO = "acme/payments"
START = datetime(2026, 5, 1, tzinfo=UTC)
TABLES = (
    "context_event_links",
    "context_repo_scopes",
    "canonical_contexts",
    "events",
    "audit_log_entries",
)


@dataclass
class World:
    tenant_id: UUID
    engine: AsyncEngine
    redis: Redis


async def _admin(sql: str, params: dict[str, Any] | None = None) -> None:
    engine = create_async_engine(ADMIN_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(text(sql), params or {})
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(name="w")
async def world(async_redis_client: Redis) -> AsyncGenerator[World, None]:
    tenant_id = uuid4()
    role, url = await create_app_role()
    engine = create_checkcord_engine(url)
    try:
        with app_database_url(url):
            yield World(tenant_id, engine, async_redis_client)
    finally:
        await engine.dispose()
        await drop_app_role(role)
        for table in TABLES:
            await _admin(f"DELETE FROM {table} WHERE tenant_id = :t", {"t": tenant_id})


async def _record(
    w: World,
    summary: str,
    *,
    severity: str = "hard_block",
    repo: str | None = None,
    day: int = 0,
    retired: bool = False,
    held: bool = False,
    context_type: str = "general",
    base_event: UUID | None = None,
) -> UUID:
    record = uuid4()
    await _admin(
        "INSERT INTO canonical_contexts (id, tenant_id, summary, severity, access_control, "
        "created_at, retired_at, held_connection, context_type, base_event_id) VALUES (:id, "
        ":t, :s, :sev, '{\"is_public\": true}', :at, :retired, :held, :ct, :base)",
        {
            "id": record,
            "t": w.tenant_id,
            "s": summary,
            "sev": severity,
            "ct": context_type,
            "base": base_event,
            "at": START + timedelta(days=day),
            "retired": START if retired else None,
            "held": "jira:held.example.net" if held else None,
        },
    )
    if repo:
        await _admin(
            "INSERT INTO context_repo_scopes (canonical_id, scope_type, scope_value, tenant_id) "
            "VALUES (:c, 'repo', :r, :t)",
            {"c": record, "r": repo, "t": w.tenant_id},
        )
    return record


async def _github_event(w: World, code_metadata: dict[str, Any]) -> UUID:
    event = uuid4()
    await _admin(
        "INSERT INTO events (event_id, tenant_id, source_platform, source_type, event_action, "
        "source_id, happened_at, ingested_at, author, content_raw, content_clean, "
        "access_allowed_ids, access_control, fingerprint, schema_version, version, "
        "code_metadata) VALUES (:e, :t, 'github', 'pull_request', 'created', :sid, :at, :at, "
        "'{\"source_id\": \"u1\"}', 'x', 'x', '{}', '{\"is_public\": true}', :sid, 1, 1, "
        "CAST(:meta AS jsonb))",
        {
            "e": event,
            "t": w.tenant_id,
            "sid": event.hex,
            "at": START,
            "meta": json.dumps(code_metadata),
        },
    )
    return event


async def _link(w: World, record: UUID, event: UUID) -> None:
    await _admin(
        "INSERT INTO context_event_links (canonical_id, event_id, tenant_id) VALUES (:c, :e, :t)",
        {"c": record, "e": event, "t": w.tenant_id},
    )


async def _check(w: World, judge: FakeJudge, linked: list[UUID] | None = None) -> FakePullRequest:
    github = FakePullRequest()
    payload = {
        "repository_full_name": REPO,
        "pull_request_number": 9,
        "head_sha": uuid4().hex,
        "tenant_id": str(w.tenant_id),
        "diff": "+    return float(total)",
        "affected_contexts": [
            {
                "symbol_name": "quote_total",
                "file_path": "quotes.py",
                "node_type": "function_definition",
                "change_type": "modified",
                "canonical_ids": [str(r) for r in linked or []],
            }
        ],
    }
    with (
        patch("checkcord.workers.pr_status_reporter.github_token_for", AsyncMock(return_value="t")),
        patch("checkcord.workers.pr_status_reporter.GitHubRestClient", return_value=github),
    ):
        await PRStatusWorker(w.redis, w.engine, judge)._process_message(
            "ci.pr.context_checked", "g", "1-0", {"payload": json.dumps(payload)}
        )
    return github


async def _checks(w: World) -> list[dict[str, Any]]:
    engine = create_async_engine(ADMIN_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            rows = await conn.execute(
                text("SELECT params FROM audit_log_entries WHERE tenant_id = :t AND action = :a"),
                {"t": w.tenant_id, "a": audit.PR_CHECK},
            )
            return [r.params for r in rows]
    finally:
        await engine.dispose()


async def test_blocking_records_in_scope_are_judged_without_a_link(w: World) -> None:
    org = await _record(w, "ORG never use floats for money")
    scoped = await _record(w, "REPO quotes are rounded half-even", repo=REPO, day=1)
    await _record(w, "OTHER-REPO use tabs", repo="acme/elsewhere", day=2)
    await _record(w, "ADVISORY prefer small functions", severity="soft_warn", day=3)
    await _record(w, "RETIRED floats are fine", retired=True, day=4)
    await _record(w, "HELD floats are fine", held=True, day=5)
    judge = FakeJudge(lambda t, _d: ("conflicts" if t.startswith("ORG") else "unrelated", "why"))

    github = await _check(w, judge)

    assert sorted(t.split()[0] for t, _ in judge.calls) == ["ORG", "REPO"]
    assert github.statuses[-1] == (
        "failure",
        "Conflicts with 1 blocking decision(s). See the Checkcord comment.",
    )
    assert "ORG never use floats" in github.body
    assert "REPO quotes" not in github.body
    [check] = await _checks(w)
    assert check["verdicts"] == {str(org): "conflicts", str(scoped): "unrelated"}
    assert check["blocked"] is True and check["conflicts"] == 1


async def test_retired_and_held_records_are_not_candidates_even_when_linked(w: World) -> None:
    retired = await _record(w, "RETIRED floats are fine", retired=True)
    held = await _record(w, "HELD floats are fine", held=True)
    judge = FakeJudge()

    github = await _check(w, judge, linked=[retired, held])

    assert judge.calls == []
    assert github.statuses == [("success", "No conflicts with recorded decisions.")]
    assert github.comments == {}


async def test_linked_records_are_judged_once_and_listed_with_their_symbol(w: World) -> None:
    linked = await _record(w, "LINKED money is integer cents")
    judge = FakeJudge("complies")

    github = await _check(w, judge, linked=[linked])

    assert len(judge.calls) == 1
    assert "- `quote_total`: LINKED money is integer cents" in github.body
    assert github.statuses[-1][0] == "success"


async def test_only_the_newest_in_scope_records_are_judged(w: World) -> None:
    for day in range(SCOPE_CANDIDATES + 2):
        await _record(w, f"RULE-{day:02d} keep it simple", day=day)
    judge = FakeJudge("unrelated")

    github = await _check(w, judge)

    judged = sorted(t.split()[0] for t, _ in judge.calls)
    assert judged == [f"RULE-{d:02d}" for d in range(2, SCOPE_CANDIDATES + 2)]
    assert github.body.endswith(
        f"2 more blocking decision(s) in scope were not checked; only the newest "
        f"{SCOPE_CANDIDATES} were."
    )


async def test_linked_reference_records_are_not_judged_unless_blocking(w: World) -> None:
    reference = await _record(w, "REFERENCE ticket text", severity="soft_warn")
    decision = await _record(w, "DECISION money is cents", severity="soft_warn", context_type="adr")
    judge = FakeJudge("complies")

    github = await _check(w, judge, linked=[reference, decision])

    assert [t.split()[0] for t, _ in judge.calls] == ["DECISION"]
    assert "REFERENCE" not in github.body


async def test_a_pull_requests_own_records_are_never_candidates(w: World) -> None:
    own = await _github_event(w, {"repository": REPO, "pull_request_id": "9"})
    other = await _github_event(w, {"repository": REPO, "pull_request_id": "8"})
    made_here = await _record(w, "OWN floats are fine now", base_event=own)
    linked_here = await _record(w, "LINKED floats are fine", day=1, base_event=other)
    await _link(w, linked_here, own)
    elsewhere = await _record(w, "OTHER money is cents", day=2, base_event=other)
    judge = FakeJudge("conflicts")

    github = await _check(w, judge, linked=[made_here, linked_here, elsewhere])

    assert [t.split()[0] for t, _ in judge.calls] == ["OTHER"]
    assert "OWN" not in github.body and "LINKED floats" not in github.body

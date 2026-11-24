import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app_role_helpers import create_app_role, drop_app_role
from key_helpers import KeySpec, seed_context, seeded_keys
from pr_check_fakes import FakeJudge
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkcord.ast.hasher import HashedNode
from checkcord.core.config import get_settings
from checkcord.workers.context_state_compiler import ContextStateCompilerWorker
from checkcord.workers.pr_evaluator import PRWorker
from checkcord.workers.pr_status_reporter import PRStatusWorker

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

EMAIL = "oncall@acme.example"
TOKEN = "ghp_" + "A1b2C3d4" * 4
SUMMARIES = {
    "public": "PUBLIC-RECORD keep the billing retries idempotent",
    "full": f"FULL-RECORD page {EMAIL} and rotate {TOKEN} before the cutover",
    "partial": "PARTIAL-RECORD the vendor contract ends in March",
    "empty": "EMPTY-RECORD the reorg moves payments to another team",
}
RESTRICTED_LINE = (
    "- Restricted record (severity: `{severity}`). Its content is visible in Checkcord"
)

_real_sleep = asyncio.sleep


async def _short_sleep(_seconds: float) -> None:
    await _real_sleep(0.001)


@dataclass
class FakeGitHub:
    logins: list[str] | Exception
    private: bool = True
    comments: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)

    async def get_repository(self, owner: str, repo: str) -> dict[str, Any]:
        visibility = "private" if self.private else "public"
        return {"private": self.private, "visibility": visibility, "default_branch": "main"}

    async def get_collaborators(self, owner: str, repo: str) -> list[str]:
        if isinstance(self.logins, Exception):
            raise self.logins
        return list(self.logins)

    async def create_issue_comment(self, **kwargs: Any) -> bool:
        self.comments.append(kwargs["body"])
        return True

    async def list_issue_comments(self, owner: str, repo: str, n: int) -> list[dict[str, Any]]:
        return []

    async def update_issue_comment(self, owner: str, repo: str, cid: int, body: str) -> bool:
        return False

    async def create_commit_status(self, **kwargs: Any) -> bool:
        self.statuses.append(kwargs["state"])
        return True


@dataclass(frozen=True)
class World:
    tenant: UUID
    repo: str
    engine: AsyncEngine
    ids: dict[str, UUID]
    people: dict[str, UUID] = field(default_factory=dict)


@dataclass(frozen=True)
class Outputs:
    wire: str
    comment: str
    status: str
    architecture_md: str
    ledger: dict[str, list[str]]
    create_if_absent: bool


@pytest_asyncio.fixture
async def world() -> AsyncGenerator[World, None]:
    tenant = uuid4()
    repo = f"acme/audience-{uuid4().hex[:8]}"
    specs = [
        KeySpec(
            f"audience_{uuid4().hex}", tenant, user_email=f"{name}-{uuid4().hex[:6]}@acme.example"
        )
        for name in ("alice", "bob")
    ]
    owner = create_async_engine(get_settings().database_url)
    async with seeded_keys(specs) as users:
        alice, bob = (users[s.user_email] for s in specs if s.user_email)
        ids = {
            "public": await seed_context(tenant, SUMMARIES["public"], public=True),
            "full": await seed_context(
                tenant, SUMMARIES["full"], public=False, allowed=[alice, bob]
            ),
            "partial": await seed_context(
                tenant, SUMMARIES["partial"], public=False, allowed=[alice]
            ),
            "empty": await seed_context(tenant, SUMMARIES["empty"], public=False),
        }
        async with owner.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            for login, user_id in (("alice-gh", alice), ("bob-gh", bob)):
                await conn.execute(
                    text(
                        "INSERT INTO chat_users "
                        "(tenant_id, platform, native_id, display_name, internal_id) "
                        "VALUES (:t, 'github', :login, :login, :uid)"
                    ),
                    {"t": tenant, "login": login, "uid": user_id},
                )
            await conn.execute(
                text("UPDATE canonical_contexts SET severity = 'hard_block' WHERE id = :id"),
                {"id": ids["empty"]},
            )
            for name, ctx_id in ids.items():
                node_id = uuid4()
                await conn.execute(
                    text(
                        "INSERT INTO ast_nodes (id, tenant_id, repo_identifier, file_path, "
                        "symbol_name, node_type, merkle_hash) "
                        "VALUES (:id, :t, :repo, :path, :symbol, 'function_definition', 'old')"
                    ),
                    {
                        "id": node_id,
                        "t": tenant,
                        "repo": repo,
                        "path": f"src/{name}.py",
                        "symbol": f"{name}_fn",
                    },
                )
                await conn.execute(
                    text(
                        "INSERT INTO ast_context_edges "
                        "(ast_node_id, canonical_id, tenant_id, status) "
                        "VALUES (:node, :ctx, :t, 'active')"
                    ),
                    {"node": node_id, "ctx": ctx_id, "t": tenant},
                )
        role, url = await create_app_role()
        engine = create_async_engine(url)
        try:
            yield World(tenant, repo, engine, ids, {"alice": alice, "bob": bob})
        finally:
            await engine.dispose()
            await drop_app_role(role)
            async with owner.begin() as conn:
                await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
                for table in (
                    "ast_context_edges",
                    "ast_nodes",
                    "context_event_links",
                    "canonical_contexts",
                    "events",
                    "chat_users",
                ):
                    await conn.execute(
                        text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant}
                    )
            await owner.dispose()


async def _compile(world: World, redis: Redis, github: FakeGitHub) -> dict[str, Any]:
    compiler = ContextStateCompilerWorker(redis, world.engine)
    compiler.pr_service = AsyncMock()
    compiler.pr_service.propose_context_update.return_value = True
    with (
        patch("checkcord.workers.context_state_compiler.GitHubRestClient", return_value=github),
        patch(
            "checkcord.workers.context_state_compiler.github_token_for",
            AsyncMock(return_value="token"),
        ),
        patch("checkcord.workers.context_state_compiler.asyncio.sleep", side_effect=_short_sleep),
    ):
        await compiler._debounced_compile(world.tenant, world.repo)
    kwargs: dict[str, Any] = compiler.pr_service.propose_context_update.call_args.kwargs
    return kwargs


async def _run(world: World, redis: Redis, github: FakeGitHub) -> Outputs:
    nodes = [
        (
            f"src/{name}.py",
            HashedNode(
                node_type="function_definition",
                symbol_name=f"{name}_fn",
                start_byte=0,
                end_byte=10,
                content="def f(): ...",
                merkle_hash="new",
            ),
        )
        for name in world.ids
    ]
    with (
        patch("checkcord.workers.pr_evaluator.PREvaluatorService") as evaluator_service,
        patch("checkcord.workers.pr_evaluator.github_token_for", AsyncMock(return_value="token")),
        patch(
            "checkcord.workers.pr_status_reporter.github_token_for",
            AsyncMock(return_value="token"),
        ),
        patch("checkcord.workers.pr_status_reporter.GitHubRestClient", return_value=github),
    ):
        evaluator_service.return_value.evaluate_pr = AsyncMock(
            return_value={"results": nodes, "analyzed_files": [path for path, _ in nodes]}
        )
        await PRWorker(redis, db_engine=world.engine)._process_message(
            "ci.pr.evaluate",
            "g",
            "1-0",
            {
                "repository_full_name": world.repo,
                "pull_request_number": "7",
                "head_sha": "headsha",
                "tenant_id": str(world.tenant),
            },
        )
        entries = await redis.xrange("ci.pr.context_checked")
        wire = entries[-1][1][b"payload"].decode()
        await PRStatusWorker(redis, world.engine, FakeJudge())._process_message(
            "ci.pr.context_checked", "g", "1-0", {"payload": wire}
        )
    compiled = await _compile(world, redis, github)
    return Outputs(
        wire=wire,
        comment=github.comments[-1],
        status=github.statuses[-1],
        architecture_md=compiled["architecture_md"],
        ledger=json.loads(compiled["ledger_json"]),
        create_if_absent=compiled["create_if_absent"],
    )


def _shown(text_: str) -> set[str]:
    return {name for name, summary in SUMMARIES.items() if summary.split()[0] in text_}


def _ledger_ids(out: Outputs) -> set[str]:
    return {cid for ids in out.ledger.values() for cid in ids}


async def test_only_records_every_collaborator_may_see_are_written(
    world: World, async_redis_client: Redis
) -> None:
    out = await _run(world, async_redis_client, FakeGitHub(["alice-gh", "bob-gh"]))

    assert _shown(out.comment) == {"public", "full"}
    assert _shown(out.architecture_md) == {"public", "full"}
    assert _ledger_ids(out) == {str(world.ids["public"]), str(world.ids["full"])}
    assert out.create_if_absent is True

    assert RESTRICTED_LINE.format(severity="soft_warn") in out.comment
    assert RESTRICTED_LINE.format(severity="hard_block") in out.comment
    for name in ("partial", "empty"):
        assert f"node=context%3A{world.ids[name]}" in out.comment
        assert f"{name}_fn" not in out.comment
        assert f"src/{name}.py" not in out.comment
        assert str(world.ids[name]) not in out.architecture_md
    assert out.status == "failure"


async def test_text_written_to_github_is_redacted(world: World, async_redis_client: Redis) -> None:
    out = await _run(world, async_redis_client, FakeGitHub(["alice-gh", "bob-gh"]))

    for written in (out.comment, out.architecture_md):
        assert "FULL-RECORD" in written
        assert EMAIL not in written
        assert TOKEN not in written
        assert "[REDACTED:EMAIL]" in written
        assert "[REDACTED:TOKEN]" in written


async def test_unmapped_collaborator_leaves_only_public_records(
    world: World, async_redis_client: Redis
) -> None:
    github = FakeGitHub(["alice-gh", "bob-gh", "contractor-gh"])
    out = await _run(world, async_redis_client, github)

    assert _shown(out.comment) == {"public"}
    assert _shown(out.architecture_md) == {"public"}
    assert _ledger_ids(out) == {str(world.ids["public"])}
    assert out.comment.count("- Restricted record") == 3
    assert out.status == "failure"


@pytest.mark.parametrize("logins", [[], RuntimeError("github unavailable")])
async def test_failed_collaborator_lookup_leaves_only_public_records(
    world: World, async_redis_client: Redis, logins: list[str] | Exception
) -> None:
    out = await _run(world, async_redis_client, FakeGitHub(logins))

    assert _shown(out.comment) == {"public"}
    assert _shown(out.architecture_md) == {"public"}
    assert out.status == "failure"


async def test_repository_that_is_not_private_gets_only_public_records(
    world: World, async_redis_client: Redis
) -> None:
    github = FakeGitHub(["alice-gh", "bob-gh"], private=False)
    out = await _run(world, async_redis_client, github)

    assert _shown(out.comment) == {"public"}
    assert _shown(out.architecture_md) == {"public"}


async def test_stream_payload_carries_no_record_text(
    world: World, async_redis_client: Redis
) -> None:
    out = await _run(world, async_redis_client, FakeGitHub(["alice-gh", "bob-gh"]))

    assert _shown(out.wire) == set()
    for summary in SUMMARIES.values():
        assert summary not in out.wire
    for ctx_id in world.ids.values():
        assert str(ctx_id) in out.wire


async def test_record_that_stops_passing_leaves_the_compiled_file(
    world: World, async_redis_client: Redis
) -> None:
    github = FakeGitHub(["alice-gh", "bob-gh"])
    before = await _compile(world, async_redis_client, github)
    assert "FULL-RECORD" in before["architecture_md"]

    owner = create_async_engine(get_settings().database_url)
    try:
        async with owner.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text(
                    "UPDATE canonical_contexts SET access_control = "
                    "jsonb_set(access_control, '{allowed_identity_ids}', '[]'::jsonb) "
                    "WHERE id = :id"
                ),
                {"id": world.ids["full"]},
            )
            await conn.execute(
                text("DELETE FROM canonical_contexts WHERE id = :id"), {"id": world.ids["public"]}
            )
    finally:
        await owner.dispose()

    after = await _compile(world, async_redis_client, github)
    assert _shown(after["architecture_md"]) == set()
    assert json.loads(after["ledger_json"]) == {}
    assert after["create_if_absent"] is False


async def test_collaborator_added_later_narrows_the_next_write(
    world: World, async_redis_client: Redis
) -> None:
    github = FakeGitHub(["alice-gh", "bob-gh"])
    before = await _compile(world, async_redis_client, github)
    assert _shown(before["architecture_md"]) == {"public", "full"}

    github.logins = ["alice-gh", "bob-gh", "contractor-gh"]
    after = await _compile(world, async_redis_client, github)
    assert _shown(after["architecture_md"]) == {"public"}


async def _update(world: World, text_: str, *readers: UUID) -> None:
    from datetime import UTC, datetime

    from checkcord.core.redaction import StoredRedaction
    from checkcord.models.common import AccessControl, AuthorInfo
    from checkcord.models.enums import SourcePlatform, SourceType
    from checkcord.models.event import CanonicalEvent
    from checkcord.storage.postgres import PostgresEventStore, tenant_context

    event = CanonicalEvent(
        tenant_id=world.tenant,
        source_platform=SourcePlatform.SLACK,
        source_type=SourceType.MESSAGE,
        source_id=f"update:{uuid4().hex[:8]}",
        happened_at=datetime.now(UTC),
        author=AuthorInfo(source_id="someone"),
        content_raw=text_,
        content_clean=text_,
        access_control=AccessControl(is_public=False, allowed_identity_ids=list(readers)),
    )
    owner = create_async_engine(get_settings().database_url)
    try:
        async with tenant_context(owner, world.tenant) as conn:
            store = PostgresEventStore(conn)
            assert await store.save_event(event)
            await store.link_context_to_event(
                world.tenant, world.ids["public"], event.event_id, StoredRedaction(text_, "1:test")
            )
    finally:
        await owner.dispose()


async def test_an_update_is_written_only_if_every_collaborator_may_read_it(
    world: World, async_redis_client: Redis
) -> None:
    await _update(world, "SHARED-UPDATE retries back off", *world.people.values())
    await _update(world, "ALICE-ONLY-UPDATE retries stop at five", world.people["alice"])
    out = await _run(world, async_redis_client, FakeGitHub(["alice-gh", "bob-gh"]))

    assert "public" in _shown(out.architecture_md)
    assert "SHARED-UPDATE" in out.architecture_md
    assert "ALICE-ONLY-UPDATE" not in out.architecture_md
    assert "SHARED-UPDATE" in out.comment
    assert "ALICE-ONLY-UPDATE" not in out.comment

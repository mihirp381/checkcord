import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from checkcord.ast.hasher import HashedNode
from checkcord.models.pr_checks import PRContextCheckedPayload
from checkcord.services.context_matcher import ContextMatcherService
from checkcord.workers.pr_evaluator import (
    DIFF_CHARS,
    FILE_DIFF_CHARS,
    PREvaluatorService,
    PRWorker,
    build_diff,
    get_language_from_filename,
)

pytestmark = pytest.mark.asyncio


def _files(*names: str) -> list[dict]:
    return [{"filename": n, "patch": f"@@ -1 +1 @@\n-old {n}\n+new {n}"} for n in names]


async def test_build_diff_limits():
    small = build_diff([{"filename": "a.py", "patch": "+x"}, {"filename": "logo.png"}])
    assert "--- a/a.py\n+++ b/a.py\n+x" in small
    assert "Changed without a diff: logo.png" in small

    long = build_diff([{"filename": "big.py", "patch": "x" * (FILE_DIFF_CHARS + 50)}])
    assert "[cut: 50 more characters of big.py]" in long

    many = build_diff([{"filename": f"f{i}.py", "patch": "y" * 5000} for i in range(40)])
    assert len(many) < DIFF_CHARS + 500
    assert "diff limit reached" in many
    assert "[cut: 10 more files not shown]" in many


async def test_own_branch_is_not_evaluated(token_lookup):
    redis_mock = AsyncMock()
    worker = PRWorker(redis_mock, db_engine=MagicMock())
    worker.service.evaluate_pr = AsyncMock()
    msg = {
        "repository_full_name": "owner/repo",
        "pull_request_number": "3",
        "head_sha": "s",
        "head_ref": "checkcord/update-decisions",
        "tenant_id": str(uuid4()),
    }
    await worker._process_message("ci.pr.evaluate", "g", "1-0", msg)

    worker.service.evaluate_pr.assert_not_awaited()
    published = PRContextCheckedPayload.model_validate_json(
        redis_mock.xadd.call_args.kwargs["fields"]["payload"]
    )
    assert published.head_ref == "checkcord/update-decisions"
    assert published.affected_contexts == []


@pytest.fixture(autouse=True)
def token_lookup(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    lookup = AsyncMock(return_value="token")
    monkeypatch.setattr("checkcord.workers.pr_evaluator.github_token_for", lookup)
    return lookup


async def test_get_language_from_filename():
    assert get_language_from_filename("foo.py") == "python"
    assert get_language_from_filename("bar.ts") == "typescript"
    assert get_language_from_filename("baz.tsx") == "typescript"
    assert get_language_from_filename("qux.js") == "javascript"
    assert get_language_from_filename("quux.go") == "go"
    assert get_language_from_filename("Foo.java") == "java"
    assert get_language_from_filename("readme.md") is None


@patch("checkcord.workers.pr_evaluator.GitHubRestClient")
async def test_evaluate_pr_success(MockGitHubRestClient):
    client_mock = MockGitHubRestClient.return_value
    client_mock.list_pull_request_files = AsyncMock(
        return_value=_files("main.py", "README.md", "app.ts")
    )

    # We return python code for main.py, and typescript code for app.ts
    def get_file_content_mock(owner, repo, path, ref):
        if path == "main.py":
            return b"def hello_world():\n    pass\n"
        elif path == "app.ts":
            return b"export function greet(): string { return 'hi'; }\n"
        return None

    client_mock.get_file_content = AsyncMock(side_effect=get_file_content_mock)

    service = PREvaluatorService()
    eval_res = await service.evaluate_pr("owner/repo", 42, "headsha", "token")
    results = eval_res["results"]
    analyzed_files = eval_res["analyzed_files"]

    # Should have extracted hello_world from main.py and greet from app.ts
    # (README.md is ignored)
    assert len(results) == 2
    assert len(analyzed_files) == 2
    assert "main.py" in analyzed_files
    assert "app.ts" in analyzed_files

    paths = [r[0] for r in results]
    symbols = [r[1].symbol_name for r in results]
    hashes = [r[1].merkle_hash for r in results]

    assert "main.py" in paths
    assert "app.ts" in paths
    assert "hello_world" in symbols
    assert "greet" in symbols

    # Assert that hashes are non-empty sha256 strings (length 64)
    for h in hashes:
        assert isinstance(h, str)
        assert len(h) == 64


@patch("checkcord.workers.pr_evaluator.GitHubRestClient")
async def test_evaluate_pr_success_java(MockGitHubRestClient):
    client_mock = MockGitHubRestClient.return_value
    client_mock.list_pull_request_files = AsyncMock(
        return_value=_files("Greeter.java", "README.md")
    )

    def get_file_content_mock(owner, repo, path, ref):
        if path == "Greeter.java":
            return (
                b"public class Greeter {\n"
                b"    public String greet(String name) {\n"
                b'        return "hello " + name;\n'
                b"    }\n"
                b"}\n"
            )
        return None

    client_mock.get_file_content = AsyncMock(side_effect=get_file_content_mock)

    service = PREvaluatorService()
    eval_res = await service.evaluate_pr("owner/repo", 42, "headsha", "token")
    results = eval_res["results"]
    analyzed_files = eval_res["analyzed_files"]

    # README.md is ignored (get_language_from_filename returns None for it);
    # Greeter.java yields one class node and one method node.
    assert len(results) == 2
    assert analyzed_files == ["Greeter.java"]

    symbols = {r[1].symbol_name: r[1] for r in results}
    assert symbols["Greeter"].node_type == "class_definition"
    assert symbols["greet"].node_type == "method_definition"

    for _, node in results:
        assert isinstance(node.merkle_hash, str)
        assert len(node.merkle_hash) == 64


@patch("checkcord.workers.pr_evaluator.GitHubRestClient")
async def test_evaluate_pr_deleted_file(MockGitHubRestClient):
    client_mock = MockGitHubRestClient.return_value
    client_mock.list_pull_request_files = AsyncMock(return_value=_files("deleted.py"))
    client_mock.get_file_content = AsyncMock(return_value=None)  # 404/deleted file

    service = PREvaluatorService()
    eval_res = await service.evaluate_pr("owner/repo", 42, "headsha", "token")
    results = eval_res["results"]

    # Should gracefully return empty results without raising an exception
    assert results == []


@patch("checkcord.workers.pr_evaluator.GitHubRestClient")
async def test_evaluate_pr_exceeds_cap(MockGitHubRestClient):
    client_mock = MockGitHubRestClient.return_value
    # Return 60 py files
    mock_files = [f"file_{i}.py" for i in range(60)]
    client_mock.list_pull_request_files = AsyncMock(return_value=_files(*mock_files))
    client_mock.get_file_content = AsyncMock(return_value=b"def func():\n    pass")

    service = PREvaluatorService()
    eval_res = await service.evaluate_pr("owner/repo", 42, "headsha", "token")
    results = eval_res["results"]

    # Should only process 50 files
    assert len(results) == 50
    # Verify that get_file_content was called exactly 50 times
    assert client_mock.get_file_content.call_count == 50


@pytest.mark.integration
@patch("checkcord.workers.pr_evaluator.PREvaluatorService")
async def test_worker_consume_success(MockPREvaluatorService, async_redis_client):
    service_mock = MockPREvaluatorService.return_value
    service_mock.evaluate_pr = AsyncMock(
        return_value={
            "results": [
                (
                    "main.py",
                    HashedNode(
                        node_type="function_definition",
                        symbol_name="hello_world",
                        start_byte=0,
                        end_byte=25,
                        content="def hello_world():\n    pass",
                        merkle_hash="1" * 64,
                    ),
                )
            ],
            "analyzed_files": ["main.py"],
        }
    )

    worker = PRWorker(async_redis_client)

    # Push test message to stream
    payload = {
        "repository_full_name": "owner/repo",
        "pull_request_number": "42",
        "head_sha": "headsha",
        "action": "opened",
    }
    await async_redis_client.xadd("ci.pr.evaluate", fields=payload)

    # Start worker in background
    task = asyncio.create_task(worker.start())
    await asyncio.sleep(0.5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Verify service was invoked with payload parameters
    service_mock.evaluate_pr.assert_called_once_with("owner/repo", 42, "headsha", "token")

    # Verify stream message was ACKed
    groups = await async_redis_client.xinfo_groups("ci.pr.evaluate")
    assert len(groups) == 1
    assert groups[0]["name"].decode("utf-8") == "pr_evaluation_group"
    assert groups[0]["pending"] == 0


@pytest.mark.integration
@patch("checkcord.workers.pr_evaluator.PREvaluatorService")
async def test_worker_consume_failure_dlq(MockPREvaluatorService, async_redis_client):
    service_mock = MockPREvaluatorService.return_value
    service_mock.evaluate_pr = AsyncMock(side_effect=Exception("Database Connection Timeout"))

    worker = PRWorker(async_redis_client)

    # Push test message to stream
    payload = {
        "repository_full_name": "owner/repo",
        "pull_request_number": "42",
        "head_sha": "headsha",
        "action": "opened",
    }
    await async_redis_client.xadd("ci.pr.evaluate", fields=payload)

    # Start worker in background
    task = asyncio.create_task(worker.start())
    await asyncio.sleep(0.5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Verify message was routed to DLQ
    dlq_messages = await async_redis_client.xread(streams={"ci.pr.evaluate.dlq": "0-0"}, count=10)
    assert dlq_messages
    dlq_payload = dlq_messages[0][1][0][1]
    assert dlq_payload[b"repository_full_name"] == b"owner/repo"
    assert b"dlq_error" in dlq_payload
    assert b"Database Connection Timeout" in dlq_payload[b"dlq_error"]

    # Verify original message is acknowledged/removed from pending
    groups = await async_redis_client.xinfo_groups("ci.pr.evaluate")
    assert groups[0]["pending"] == 0


@pytest.mark.integration
@patch("checkcord.workers.pr_evaluator.PREvaluatorService")
async def test_worker_reclaims_a_message_a_stopped_process_left(
    MockPREvaluatorService, async_redis_client
):
    service_mock = MockPREvaluatorService.return_value
    service_mock.evaluate_pr = AsyncMock(return_value={"results": [], "analyzed_files": []})
    worker = PRWorker(async_redis_client)
    worker.settings = worker.settings.model_copy(update={"redis_stream_reclaim_min_idle_ms": 0})
    await worker._ensure_group("ci.pr.evaluate", "pr_evaluation_group")
    fields = {"repository_full_name": "owner/repo", "pull_request_number": "7", "head_sha": "s"}
    await async_redis_client.xadd("ci.pr.evaluate", fields=fields)
    await async_redis_client.xreadgroup(
        "pr_evaluation_group", "stopped-process", {"ci.pr.evaluate": ">"}
    )

    task = asyncio.create_task(worker.start())
    await asyncio.sleep(0.5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    service_mock.evaluate_pr.assert_called_once_with("owner/repo", 7, "s", "token")
    groups = await async_redis_client.xinfo_groups("ci.pr.evaluate")
    assert groups[0]["pending"] == 0


async def test_context_matcher_service_drift_and_deletion():
    tenant_id = uuid4()
    repo_identifier = "owner/repo"
    file_paths = ["main.py", "app.ts"]

    canonical_id_1 = uuid4()
    canonical_id_2 = uuid4()

    db_rows = [
        {
            "ast_node_id": uuid4(),
            "file_path": "main.py",
            "symbol_name": "hello",
            "node_type": "function_definition",
            "merkle_hash": "old_hash_1",
            "canonical_id": canonical_id_1,
        },
        {
            "ast_node_id": uuid4(),
            "file_path": "app.ts",
            "symbol_name": "greet",
            "node_type": "function_definition",
            "merkle_hash": "old_hash_2",
            "canonical_id": canonical_id_2,
        },
        {
            "ast_node_id": uuid4(),
            "file_path": "main.py",
            "symbol_name": "unchanged",
            "node_type": "function_definition",
            "merkle_hash": "same_hash",
            "canonical_id": uuid4(),
        },
    ]

    store_mock = AsyncMock()
    store_mock.find_ast_nodes_by_paths = AsyncMock(return_value=db_rows)

    pr_nodes = [
        (
            "main.py",
            HashedNode(
                node_type="function_definition",
                symbol_name="hello",
                start_byte=0,
                end_byte=25,
                content="def hello():...",
                merkle_hash="new_hash_1",
            ),
        ),
        (
            "main.py",
            HashedNode(
                node_type="function_definition",
                symbol_name="unchanged",
                start_byte=26,
                end_byte=50,
                content="def unchanged():...",
                merkle_hash="same_hash",
            ),
        ),
        (
            "main.py",
            HashedNode(
                node_type="function_definition",
                symbol_name="new_func",
                start_byte=51,
                end_byte=80,
                content="def new_func():...",
                merkle_hash="new_hash_3",
            ),
        ),
    ]

    matcher = ContextMatcherService()
    affected = await matcher.match_contexts(
        tenant_id=tenant_id,
        repo_identifier=repo_identifier,
        file_paths=file_paths,
        pr_nodes=pr_nodes,
        store=store_mock,
    )

    assert len(affected) == 2

    modified_item = next(x for x in affected if x["symbol_name"] == "hello")
    assert modified_item["change_type"] == "modified"
    assert modified_item["file_path"] == "main.py"
    assert modified_item["canonical_ids"] == [str(canonical_id_1)]

    deleted_item = next(x for x in affected if x["symbol_name"] == "greet")
    assert deleted_item["change_type"] == "deleted"
    assert deleted_item["file_path"] == "app.ts"
    assert deleted_item["canonical_ids"] == [str(canonical_id_2)]


@pytest.mark.integration
@patch("checkcord.workers.pr_evaluator.PREvaluatorService")
async def test_worker_publishes_context_checked(MockPREvaluatorService, async_redis_client):
    service_mock = MockPREvaluatorService.return_value
    service_mock.evaluate_pr = AsyncMock(
        return_value={
            "results": [
                (
                    "main.py",
                    HashedNode(
                        node_type="function_definition",
                        symbol_name="hello",
                        start_byte=0,
                        end_byte=25,
                        content="def hello():\n    pass",
                        merkle_hash="new_hash",
                    ),
                )
            ],
            "analyzed_files": ["main.py"],
        }
    )

    db_engine_mock = MagicMock()

    worker = PRWorker(async_redis_client, db_engine=db_engine_mock)

    affected_mock = [
        {
            "symbol_name": "hello",
            "file_path": "main.py",
            "node_type": "function_definition",
            "change_type": "modified",
            "canonical_ids": ["11111111-1111-1111-1111-111111111111"],
        }
    ]

    worker.matcher.match_contexts = AsyncMock(return_value=affected_mock)

    tenant_id = "00000000-0000-0000-0000-000000000001"
    payload = {
        "repository_full_name": "owner/repo",
        "pull_request_number": "42",
        "head_sha": "headsha",
        "tenant_id": tenant_id,
        "action": "opened",
    }
    await async_redis_client.xadd("ci.pr.evaluate", fields=payload)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_tenant_context(engine, t_id):
        yield MagicMock()

    with patch("checkcord.storage.postgres.tenant_context", side_effect=mock_tenant_context):
        task = asyncio.create_task(worker.start())
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    checked_messages = await async_redis_client.xread(
        streams={"ci.pr.context_checked": "0-0"}, count=10
    )
    assert checked_messages
    checked_fields = checked_messages[0][1][0][1]

    checked_payload = PRContextCheckedPayload.model_validate_json(
        checked_fields[b"payload"].decode("utf-8")
    )
    assert checked_payload.repository_full_name == "owner/repo"
    assert checked_payload.pull_request_number == 42
    assert checked_payload.head_sha == "headsha"
    assert str(checked_payload.tenant_id) == tenant_id

    assert len(checked_payload.affected_contexts) == 1
    affected = checked_payload.affected_contexts[0]
    assert affected.symbol_name == "hello"
    assert affected.change_type == "modified"
    assert [str(cid) for cid in affected.canonical_ids] == ["11111111-1111-1111-1111-111111111111"]


@patch("checkcord.workers.pr_evaluator.PREvaluatorService")
async def test_token_comes_from_lookup_not_message(MockPREvaluatorService, token_lookup):
    service_mock = MockPREvaluatorService.return_value
    service_mock.evaluate_pr = AsyncMock(return_value={"results": [], "analyzed_files": []})
    redis_mock = AsyncMock()
    tenant_id = uuid4()
    worker = PRWorker(redis_mock, db_engine=MagicMock())

    legacy = {
        "repository_full_name": "owner/repo",
        "pull_request_number": "42",
        "head_sha": "headsha",
        "tenant_id": str(tenant_id),
        "personal_access_token": "ghp_stale_in_flight",
        "action": "opened",
    }
    await worker._process_message("ci.pr.evaluate", "g", "1-0", legacy)

    token_lookup.assert_awaited_once_with(worker.db_engine, tenant_id, "owner/repo")
    service_mock.evaluate_pr.assert_awaited_once_with("owner/repo", 42, "headsha", "token")
    published = redis_mock.xadd.call_args.kwargs["fields"]["payload"]
    assert "ghp_stale_in_flight" not in published
    assert "personal_access_token" not in published
    redis_mock.xack.assert_awaited_once_with("ci.pr.evaluate", "g", "1-0")


@patch("checkcord.workers.pr_evaluator.PREvaluatorService")
async def test_dlq_copy_drops_legacy_token(MockPREvaluatorService):
    service_mock = MockPREvaluatorService.return_value
    service_mock.evaluate_pr = AsyncMock(side_effect=Exception("boom"))
    redis_mock = AsyncMock()
    worker = PRWorker(redis_mock)

    legacy = {
        "repository_full_name": "owner/repo",
        "pull_request_number": "42",
        "head_sha": "headsha",
        "personal_access_token": "ghp_stale_in_flight",
    }
    await worker._process_message("ci.pr.evaluate", "g", "1-0", legacy)

    fields = redis_mock.xadd.call_args.kwargs["fields"]
    assert redis_mock.xadd.call_args.kwargs["name"] == "ci.pr.evaluate.dlq"
    assert "personal_access_token" not in fields
    assert "ghp_stale_in_flight" not in str(fields)
    assert fields["repository_full_name"] == "owner/repo"


async def test_missing_token_routes_to_dlq(token_lookup):
    token_lookup.side_effect = LookupError("no stored GitHub token for owner/repo")
    redis_mock = AsyncMock()
    worker = PRWorker(redis_mock)

    msg = {"repository_full_name": "owner/repo", "pull_request_number": "1", "head_sha": "s"}
    await worker._process_message("ci.pr.evaluate", "g", "1-0", msg)

    assert redis_mock.xadd.call_args.kwargs["name"] == "ci.pr.evaluate.dlq"
    redis_mock.xack.assert_awaited_once_with("ci.pr.evaluate", "g", "1-0")

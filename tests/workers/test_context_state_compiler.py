import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.services.context_formatter import CompiledContext
from checkcord.services.repo_audience import RepoAudience
from checkcord.storage.postgres import PostgresEventStore
from checkcord.workers.context_state_compiler import ContextStateCompilerWorker

pytestmark = pytest.mark.asyncio


_real_sleep = asyncio.sleep


async def _mock_sleep(seconds: float) -> None:
    await _real_sleep(0.001)


@pytest.fixture(autouse=True)
def token_lookup(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    lookup = AsyncMock(return_value="repo-token")
    monkeypatch.setattr("checkcord.workers.context_state_compiler.github_token_for", lookup)
    return lookup


@pytest.fixture(autouse=True)
def audience_lookup(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    lookup = AsyncMock(return_value=RepoAudience())
    monkeypatch.setattr("checkcord.workers.context_state_compiler.resolve_repo_audience", lookup)
    return lookup


async def test_context_compiler_worker_process_message_success() -> None:
    # Arrange
    tenant_id = uuid4()
    context_id = uuid4()
    event_id = uuid4()
    repo_identifier = "my-org/my-repo"

    event = CanonicalEvent(
        event_id=event_id,
        tenant_id=tenant_id,
        source_platform=SourcePlatform.CUSTOM,
        source_type=SourceType.CUSTOM_PAYLOAD,
        source_id=str(context_id),
        happened_at="2026-07-02T12:00:00Z",
        author={"source_id": "system"},
        content_raw="Rule content",
        content_clean="Rule content",
        access_control={"is_public": True},
    )

    mock_redis = AsyncMock()
    mock_engine = MagicMock()
    mock_store = AsyncMock(spec=PostgresEventStore)

    # Database Mocks
    mock_store.get_active_repos_for_compilation.return_value = [repo_identifier]
    mock_store.get_active_contexts_for_repo_compilation.return_value = [
        {
            "id": context_id,
            "summary": "Some architectural rule",
            "confidence_score": 0.99,
            "created_at": datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC),
            "access_control": {"is_public": True},
        }
    ]
    mock_store.get_symbols_for_context_in_repo.return_value = []
    mock_store.get_events_for_context.return_value = []
    mock_store.get_repo_ledger_mappings.return_value = []

    # Mock DB Context
    with (
        patch("checkcord.workers.context_state_compiler.tenant_context") as mock_tenant_context,
        patch(
            "checkcord.workers.context_state_compiler.PostgresEventStore",
            return_value=mock_store,
        ),
        patch(
            "checkcord.workers.context_state_compiler.asyncio.sleep",
            side_effect=_mock_sleep,
        ) as mock_sleep,
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = AsyncMock()
        mock_tenant_context.return_value = mock_ctx

        worker = ContextStateCompilerWorker(redis_client=mock_redis, db_engine=mock_engine)
        mock_pr_service = AsyncMock()
        mock_pr_service.propose_context_update.return_value = True
        worker.pr_service = mock_pr_service

        # Act
        await worker.process_message(event)

        # Await the scheduled debounced compilation task
        assert len(worker.debounce_tasks) == 1
        task = list(worker.debounce_tasks.values())[0]
        await task

        # Assert
        # Check sleep was called (for debounce/linking wait)
        mock_sleep.assert_called_once_with(2.0)

        # Verify compilation queries were run
        mock_store.get_active_repos_for_compilation.assert_called_once_with(tenant_id)
        mock_store.get_active_contexts_for_repo_compilation.assert_called_once_with(
            tenant_id, repo_identifier
        )

        # Verify Redis key sets
        md_key = f"context:compiled:{tenant_id}:{repo_identifier}:architecture_md"
        ledger_key = f"context:compiled:{tenant_id}:{repo_identifier}:ledger_json"

        # Check set calls
        set_calls = mock_redis.set.call_args_list
        assert len(set_calls) == 2

        # Check first key and value contents
        assert set_calls[0][0][0] == md_key
        assert f"tenant_id: {tenant_id}" in set_calls[0][0][1]
        assert "### Some architectural rule" in set_calls[0][0][1]

        # Check second key and value contents
        assert set_calls[1][0][0] == ledger_key
        assert set_calls[1][0][1] == "{}"

        # Verify PR proposal was called
        mock_pr_service.propose_context_update.assert_called_once_with(
            repo_full_name=repo_identifier,
            architecture_md=set_calls[0][0][1],
            ledger_json=set_calls[1][0][1],
            pat="repo-token",
            create_if_absent=True,
        )


async def test_context_compiler_worker_debounce_storm() -> None:
    # Arrange
    tenant_id = uuid4()
    context_id = uuid4()
    event_id = uuid4()
    repo_identifier = "my-org/my-repo"

    event = CanonicalEvent(
        event_id=event_id,
        tenant_id=tenant_id,
        source_platform=SourcePlatform.CUSTOM,
        source_type=SourceType.CUSTOM_PAYLOAD,
        source_id=str(context_id),
        happened_at="2026-07-02T12:00:00Z",
        author={"source_id": "system"},
        content_raw="Rule content",
        content_clean="Rule content",
        access_control={"is_public": True},
    )

    mock_redis = AsyncMock()
    mock_engine = MagicMock()
    mock_store = AsyncMock(spec=PostgresEventStore)

    # Database Mocks
    mock_store.get_active_repos_for_compilation.return_value = [repo_identifier]
    mock_store.get_active_contexts_for_repo_compilation.return_value = []
    mock_store.get_symbols_for_context_in_repo.return_value = []
    mock_store.get_events_for_context.return_value = []
    mock_store.get_repo_ledger_mappings.return_value = []

    # Mock DB Context
    with (
        patch("checkcord.workers.context_state_compiler.tenant_context") as mock_tenant_context,
        patch(
            "checkcord.workers.context_state_compiler.PostgresEventStore",
            return_value=mock_store,
        ),
        patch(
            "checkcord.workers.context_state_compiler.asyncio.sleep",
            side_effect=_mock_sleep,
        ),
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = AsyncMock()
        mock_tenant_context.return_value = mock_ctx

        worker = ContextStateCompilerWorker(redis_client=mock_redis, db_engine=mock_engine)
        mock_pr_service = AsyncMock()
        mock_pr_service.propose_context_update.return_value = True
        worker.pr_service = mock_pr_service

        # Act: Trigger process_message 3 times sequentially (instant)
        await worker.process_message(event)
        task_1 = list(worker.debounce_tasks.values())[0]

        await worker.process_message(event)
        task_2 = list(worker.debounce_tasks.values())[0]

        # Assert: verify scheduling
        await worker.process_message(event)
        task_3 = list(worker.debounce_tasks.values())[0]

        # Assert: Verify task_1 and task_2 are cancelled, and task_3 is still running/active
        # Yield control to event loop to allow cancellations to process
        await _real_sleep(0)

        assert task_1.cancelled() is True
        assert task_2.cancelled() is True
        assert task_3.cancelled() is False

        # Await the final task
        await task_3

        # Verify compilation queries were run only once (from task_3,
        # and task_1/task_2 were cancelled before querying DB).
        # Note: get_active_repos_for_compilation is called in process_message
        # itself (so it is called 3 times), but actual compilation queries
        # are only called inside _debounced_compile.
        mock_store.get_active_contexts_for_repo_compilation.assert_called_once_with(
            tenant_id, repo_identifier
        )
        assert mock_redis.set.call_count == 2
        mock_pr_service.propose_context_update.assert_called_once()
        assert mock_pr_service.propose_context_update.call_args.kwargs["create_if_absent"] is False


async def test_context_compiler_skips_pr_without_stored_token(token_lookup: AsyncMock) -> None:
    token_lookup.side_effect = LookupError("no stored GitHub token for my-org/my-repo")
    tenant_id = uuid4()
    mock_redis = AsyncMock()
    mock_engine = MagicMock()
    with (
        patch("checkcord.workers.context_state_compiler.tenant_context") as mock_tenant_context,
        patch("checkcord.workers.context_state_compiler.PostgresEventStore"),
        patch(
            "checkcord.workers.context_state_compiler.asyncio.sleep",
            side_effect=_mock_sleep,
        ),
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = AsyncMock()
        mock_tenant_context.return_value = mock_ctx
        worker = ContextStateCompilerWorker(redis_client=mock_redis, db_engine=mock_engine)
        worker.formatter = MagicMock()
        worker.formatter.compile_for_repo = AsyncMock(
            return_value=CompiledContext("# md", "{}", [])
        )
        worker.pr_service = AsyncMock()

        await worker._debounced_compile(tenant_id, "my-org/my-repo")

    token_lookup.assert_awaited_once_with(mock_engine, tenant_id, "my-org/my-repo")
    assert mock_redis.set.await_count == 2
    worker.pr_service.propose_context_update.assert_not_called()
    assert worker.formatter.compile_for_repo.await_args.args[3] == RepoAudience()

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline.aggregator import recalculate_confidence_scores


class AsyncContextManagerMock:
    """Helper mock class for async context managers."""

    def __init__(self, mock_obj: Any) -> None:
        self.mock_obj = mock_obj

    async def __aenter__(self) -> Any:
        return self.mock_obj

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


@pytest.mark.asyncio
@patch("checkcord.pipeline.aggregator.list_tenant_ids")
@patch("checkcord.pipeline.aggregator.PostgresEventStore")
@patch("checkcord.pipeline.aggregator.tenant_context")
async def test_recalculate_confidence_scores_no_stale_contexts(
    mock_tenant_context: MagicMock,
    mock_store_cls: AsyncMock,
    mock_list_tenants: AsyncMock,
) -> None:
    """Verify that when no stale contexts exist, the aggregator stops immediately."""
    mock_engine = MagicMock()
    mock_list_tenants.return_value = [uuid4()]
    mock_tenant_context.return_value = AsyncContextManagerMock(AsyncMock())

    mock_store = AsyncMock()
    mock_store.get_stale_contexts_global.return_value = []
    mock_store_cls.return_value = mock_store

    await recalculate_confidence_scores(mock_engine)

    mock_store.get_stale_contexts_global.assert_called_once()
    mock_store.get_events_for_context.assert_not_called()
    mock_store.update_context_scores_batch_global.assert_not_called()


@pytest.mark.asyncio
@patch("checkcord.pipeline.aggregator.list_tenant_ids")
@patch("checkcord.pipeline.aggregator.PostgresEventStore")
@patch("checkcord.pipeline.aggregator.tenant_context")
async def test_recalculate_confidence_scores_success(
    mock_tenant_context: AsyncMock,
    mock_store_cls: AsyncMock,
    mock_list_tenants: AsyncMock,
) -> None:
    """Verify recalculate_confidence_scores fetches events, calculates scores, and updates db."""
    mock_engine = MagicMock()

    tenant_id = uuid4()
    context_id = uuid4()
    mock_list_tenants.return_value = [tenant_id]

    mock_store = AsyncMock()
    mock_store.get_stale_contexts_global.return_value = [(tenant_id, context_id)]
    mock_store.update_context_scores_batch_global.return_value = 1
    mock_store_cls.return_value = mock_store

    mock_tenant_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_tenant_conn
    mock_tenant_context.return_value = mock_context

    # Stub events returned from database
    event_id = uuid4()
    mock_store.get_events_for_context.return_value = [event_id]

    dummy_event = CanonicalEvent(
        tenant_id=tenant_id,
        source_platform=SourcePlatform.SLACK,
        source_type=SourceType.MESSAGE,
        source_id="msg_123",
        happened_at=datetime.now(UTC),
        author={"source_id": "user_456", "display_name": "Author"},
        content_raw="hello",
        content_clean="hello",
        access_control={"is_public": False},
    )
    mock_store.get_events_by_ids.return_value = [dummy_event]

    await recalculate_confidence_scores(mock_engine)

    # Stale check ran once, inside the tenant's context
    mock_store.get_stale_contexts_global.assert_called_once()
    mock_tenant_context.assert_called_once_with(mock_engine, tenant_id)

    # Event retrieval occurred inside tenant store context
    mock_store.get_events_for_context.assert_called_once_with(tenant_id, context_id)
    mock_store.get_events_by_ids.assert_called_once_with(tenant_id, [event_id])

    # Updates written back inside the tenant's context
    # Expected score is 2.0 (Slack base weight = 2.0, no decay, status UNPROCESSED)
    mock_store.update_context_scores_batch_global.assert_called_once_with(
        [(tenant_id, context_id, 2.0)]
    )


@pytest.mark.asyncio
@patch("checkcord.pipeline.aggregator.list_tenant_ids")
@patch("checkcord.pipeline.aggregator.PostgresEventStore")
@patch("checkcord.pipeline.aggregator.tenant_context")
async def test_recalculate_confidence_scores_graceful_tenant_error(
    mock_tenant_context: AsyncMock,
    mock_store_cls: AsyncMock,
    mock_list_tenants: AsyncMock,
) -> None:
    """Verify that a failure in one tenant does not crash the entire aggregator run."""
    mock_engine = MagicMock()

    tenant1 = uuid4()
    tenant2 = uuid4()
    context1 = uuid4()
    context2 = uuid4()
    mock_list_tenants.return_value = [tenant1, tenant2]

    mock_store = AsyncMock()
    # Each tenant's RLS-scoped query only returns that tenant's rows.
    stale_by_call = iter([[(tenant1, context1)], [(tenant2, context2)]])
    mock_store.get_stale_contexts_global.side_effect = lambda _cutoff: next(stale_by_call)
    mock_store.update_context_scores_batch_global.return_value = 1
    mock_store_cls.return_value = mock_store

    # Tenant context helper side effect
    mock_tenant_conn = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_tenant_conn
    mock_tenant_context.return_value = mock_context

    # Stub first tenant event fetch to raise error, second to succeed
    def mock_get_events(t_id: Any, c_id: Any) -> Any:
        if t_id == tenant1:
            raise RuntimeError("DB error on tenant1")
        return [uuid4()]

    mock_store.get_events_for_context.side_effect = mock_get_events
    mock_store.get_events_by_ids.return_value = [
        CanonicalEvent(
            tenant_id=tenant2,
            source_platform=SourcePlatform.JIRA,
            source_type=SourceType.TASK,
            source_id="jira_123",
            happened_at=datetime.now(UTC),
            author={"source_id": "user_456", "display_name": "Author"},
            content_raw="jira",
            content_clean="jira",
            access_control={"is_public": False},
        )
    ]

    await recalculate_confidence_scores(mock_engine)

    # Both tenant contexts should be attempted
    assert mock_tenant_context.call_count == 2

    # Update still executed, but only for tenant2 (expected score = 7.0)
    mock_store.update_context_scores_batch_global.assert_called_once_with(
        [(tenant2, context2, 7.0)]
    )

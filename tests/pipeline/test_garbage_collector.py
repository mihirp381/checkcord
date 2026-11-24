"""Unit tests for partition maintenance."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from checkcord.pipeline.garbage_collector import maintain_partitions


@pytest.mark.asyncio
@patch("checkcord.pipeline.garbage_collector.create_weekly_partitions")
async def test_maintain_partitions(mock_create_partitions: AsyncMock) -> None:
    """Verify that maintain_partitions calls create_weekly_partitions."""
    mock_engine = MagicMock()
    await maintain_partitions(mock_engine)
    mock_create_partitions.assert_called_once_with(mock_engine)


@pytest.mark.asyncio
@patch("checkcord.pipeline.garbage_collector.create_weekly_partitions")
async def test_maintain_partitions_failure(mock_create_partitions: AsyncMock) -> None:
    """Verify that maintain_partitions propagates exceptions."""
    mock_engine = MagicMock()
    mock_create_partitions.side_effect = RuntimeError("db error")
    with pytest.raises(RuntimeError, match="db error"):
        await maintain_partitions(mock_engine)

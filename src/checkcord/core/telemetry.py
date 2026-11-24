"""Counters for system metrics."""

from typing import Any

import structlog

from checkcord.core import metrics

logger = structlog.get_logger(__name__)


def increment_counter(name: str, tags: dict[str, Any] | None = None) -> None:
    """Count a pipeline-stage outcome in the metrics and log it."""
    metrics.count_stage(name, tags)
    logger.info(
        event="metric_event",
        metric_name=name,
        metric_type="counter",
        tags=tags or {},
    )

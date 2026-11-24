from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from checkcord.pipeline.scheduler import create_scheduler


def test_create_scheduler() -> None:
    """Verify that create_scheduler configures the correct jobs and triggers."""
    mock_engine = MagicMock()
    scheduler = create_scheduler(mock_engine)

    assert isinstance(scheduler, AsyncIOScheduler)
    jobs = scheduler.get_jobs()
    assert len(jobs) == 8

    job_ids = {job.id for job in jobs}
    assert job_ids == {
        "maintain_partitions",
        "recalculate_confidence_scores",
        "staleness_pruning",
        "refresh_audiences",
        "flush_model_usage",
        "renew_teams_meeting_subscriptions",
        "sync_slack_directory",
        "sync_msteams_directory",
    }

    # Validate maintain_partitions trigger (Sunday at 00:00 UTC)
    partitions_job = scheduler.get_job("maintain_partitions")
    assert partitions_job is not None
    fields = {field.name: str(field) for field in partitions_job.trigger.fields}
    assert fields["day_of_week"] == "sun"
    assert fields["hour"] == "0"
    assert fields["minute"] == "0"

    # Validate recalculate_confidence_scores trigger (interval-based)
    recalc_job = scheduler.get_job("recalculate_confidence_scores")
    assert recalc_job is not None
    assert str(recalc_job.trigger).startswith("interval[")

    # Validate staleness_pruning trigger (Sunday at 02:00 UTC, staggered from partitions)
    staleness_job = scheduler.get_job("staleness_pruning")
    assert staleness_job is not None
    fields_stale = {field.name: str(field) for field in staleness_job.trigger.fields}
    assert fields_stale["day_of_week"] == "sun"
    assert fields_stale["hour"] == "2"
    assert fields_stale["minute"] == "0"


def test_judge_batch_sweep_runs_only_in_batch_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from checkcord.core.config import get_settings

    assert create_scheduler(MagicMock()).get_job("judge_batch_sweep") is None
    monkeypatch.setenv("MEETING_JUDGE_MODE", "batch")
    get_settings.cache_clear()
    job = create_scheduler(MagicMock()).get_job("judge_batch_sweep")
    assert job is not None
    assert str(job.trigger).startswith("interval[")


def test_create_scheduler_with_redis_client_adds_poller_jobs() -> None:
    """The GitHub/Notion/Linear/Jira poller jobs only get registered when a redis_client
    is supplied (they need an EventBus to publish to)."""
    mock_engine = MagicMock()
    mock_redis_client = MagicMock()
    scheduler = create_scheduler(mock_engine, redis_client=mock_redis_client)

    job_ids = {job.id for job in scheduler.get_jobs()}
    assert job_ids == {
        "maintain_partitions",
        "recalculate_confidence_scores",
        "staleness_pruning",
        "refresh_audiences",
        "flush_model_usage",
        "renew_teams_meeting_subscriptions",
        "sync_slack_directory",
        "sync_msteams_directory",
        "poll_github_history",
        "poll_notion_workspaces",
        "poll_linear_organizations",
        "poll_jira_documents",
    }

    for job_id in ("poll_notion_workspaces", "poll_linear_organizations", "poll_jira_documents"):
        job = scheduler.get_job(job_id)
        assert job is not None
        assert str(job.trigger).startswith("interval[")


def test_github_poll_interval_is_a_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from checkcord.core.config import get_settings

    monkeypatch.setenv("GITHUB_POLL_INTERVAL_MINUTES", "2")
    get_settings.cache_clear()
    job = create_scheduler(MagicMock(), redis_client=MagicMock()).get_job("poll_github_history")
    assert job is not None
    assert job.trigger.interval.total_seconds() == 120


def test_retention_job_runs_daily_only_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from checkcord.core.config import get_settings

    assert create_scheduler(MagicMock()).get_job("purge_expired_events") is None
    monkeypatch.setenv("EVENT_RETENTION_DAYS", "90")
    get_settings.cache_clear()
    job = create_scheduler(MagicMock()).get_job("purge_expired_events")
    assert job is not None
    fields = {field.name: str(field) for field in job.trigger.fields}
    assert fields["hour"] == "3" and fields["day_of_week"] == "*"

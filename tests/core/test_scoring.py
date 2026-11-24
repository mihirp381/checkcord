from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from checkcord.core.scoring import (
    DEFAULT_HALF_LIFE_DAYS,
    HALF_LIFE_DAYS_OVERRIDES,
    calculate_confidence_score,
    get_half_life_days,
)
from checkcord.models.enums import SignalStatus, SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent


@pytest.fixture
def make_event() -> Any:
    """Helper fixture to create events with custom parameters."""

    def _make(
        platform: SourcePlatform = SourcePlatform.SLACK,
        source_type: SourceType = SourceType.MESSAGE,
        status: SignalStatus = SignalStatus.UNPROCESSED,
        happened_at: datetime | None = None,
    ) -> CanonicalEvent:
        if happened_at is None:
            happened_at = datetime.now(UTC)
        elif happened_at.tzinfo is None:
            happened_at = happened_at.replace(tzinfo=UTC)

        return CanonicalEvent(
            tenant_id=uuid4(),
            source_platform=platform,
            source_type=source_type,
            source_id=f"src_{uuid4()}",
            happened_at=happened_at,
            author={"source_id": "user_123", "display_name": "Test Author"},
            content_raw="Test content",
            content_clean="Test content",
            access_control={"is_public": False},
            signal_status=status,
        )

    return _make


def test_empty_event_list() -> None:
    """Empty list of events should yield 0.0 confidence score."""
    assert calculate_confidence_score([]) == 0.0


def test_default_platform_weights(make_event: Any) -> None:
    """Verify base weights are resolved correctly for standard platforms."""
    # Slack base weight = 2.0
    ev1 = make_event(platform=SourcePlatform.SLACK, source_type=SourceType.MESSAGE)
    assert calculate_confidence_score([ev1]) == 2.0

    # GitHub default fallback when no source_type override is found (e.g. wiki_page)
    # GITHUB default base weight = 8.0
    ev2 = make_event(platform=SourcePlatform.GITHUB, source_type=SourceType.WIKI_PAGE)
    assert calculate_confidence_score([ev2]) == 8.0

    # Jira base weight = 7.0
    ev3 = make_event(platform=SourcePlatform.JIRA, source_type=SourceType.WIKI_PAGE)
    assert calculate_confidence_score([ev3]) == 7.0


def test_source_type_overrides(make_event: Any) -> None:
    """Verify that specific source_type overrides take precedence."""
    # (GITHUB, PR_REVIEW) -> 10.0 override
    ev1 = make_event(platform=SourcePlatform.GITHUB, source_type=SourceType.PR_REVIEW)
    assert calculate_confidence_score([ev1]) == 10.0

    # (GITHUB, COMMIT) -> 9.0 override
    ev2 = make_event(platform=SourcePlatform.GITHUB, source_type=SourceType.COMMIT)
    assert calculate_confidence_score([ev2]) == 9.0

    # (SLACK, THREAD_REPLY) -> 3.0 override
    ev3 = make_event(platform=SourcePlatform.SLACK, source_type=SourceType.THREAD_REPLY)
    assert calculate_confidence_score([ev3]) == 3.0


def test_unknown_platform_fallback(make_event: Any) -> None:
    """Verify fallback weight of 1.0 is used for unrecognized platforms or types."""
    # Using a fake platform name not in enum won't pass Pydantic validation,
    # but we can check with get_event_weight directly or test CUSTOM platform.
    # Custom platform default is 1.0
    ev = make_event(platform=SourcePlatform.CUSTOM, source_type=SourceType.CUSTOM_PAYLOAD)
    assert calculate_confidence_score([ev]) == 1.0


def test_status_multipliers(make_event: Any) -> None:
    """Verify status multipliers (CONFIRMED=1.5, DENIED=0.0, EXPIRED=0.2)."""
    # Slack base weight = 2.0. CONFIRMED -> 2.0 * 1.5 = 3.0
    ev_confirmed = make_event(
        platform=SourcePlatform.SLACK,
        source_type=SourceType.MESSAGE,
        status=SignalStatus.CONFIRMED,
    )
    assert calculate_confidence_score([ev_confirmed]) == 3.0

    # Slack base weight = 2.0. DENIED -> 2.0 * 0.0 = 0.0
    ev_denied = make_event(
        platform=SourcePlatform.SLACK,
        source_type=SourceType.MESSAGE,
        status=SignalStatus.DENIED,
    )
    assert calculate_confidence_score([ev_denied]) == 0.0

    # Slack base weight = 2.0. EXPIRED -> 2.0 * 0.2 = 0.4
    ev_expired = make_event(
        platform=SourcePlatform.SLACK,
        source_type=SourceType.MESSAGE,
        status=SignalStatus.EXPIRED,
    )
    assert calculate_confidence_score([ev_expired]) == 0.4


def test_time_decay(make_event: Any) -> None:
    """Verify that exponential decay applies only after 30 days, with 90-day half-life."""
    ref_time = datetime.now(UTC)

    # 1. 20 days ago (no decay)
    ev_recent = make_event(
        platform=SourcePlatform.SLACK,
        source_type=SourceType.MESSAGE,
        happened_at=ref_time - timedelta(days=20),
    )
    assert calculate_confidence_score([ev_recent], reference_time=ref_time) == 2.0

    # 2. Exactly 30 days ago (no decay boundary)
    ev_boundary = make_event(
        platform=SourcePlatform.SLACK,
        source_type=SourceType.MESSAGE,
        happened_at=ref_time - timedelta(days=30),
    )
    assert calculate_confidence_score([ev_boundary], reference_time=ref_time) == 2.0

    # 3. 120 days ago (30 days flat + 90 days decay = 1 half-life) -> 2.0 * 0.5 = 1.0
    ev_decayed_1_hl = make_event(
        platform=SourcePlatform.SLACK,
        source_type=SourceType.MESSAGE,
        happened_at=ref_time - timedelta(days=120),
    )
    assert calculate_confidence_score([ev_decayed_1_hl], reference_time=ref_time) == 1.0

    # 4. 210 days ago (30 days flat + 180 days decay = 2 half-lives) -> 2.0 * 0.25 = 0.5
    ev_decayed_2_hl = make_event(
        platform=SourcePlatform.SLACK,
        source_type=SourceType.MESSAGE,
        happened_at=ref_time - timedelta(days=210),
    )
    assert calculate_confidence_score([ev_decayed_2_hl], reference_time=ref_time) == 0.5


def test_multiple_events_summation(make_event: Any) -> None:
    """Verify summation of multiple events."""
    ref_time = datetime.now(UTC)

    # Event 1: Slack thread reply (weight = 3.0), confirmed (*1.5 = 4.5), 10 days old -> 4.5
    ev1 = make_event(
        platform=SourcePlatform.SLACK,
        source_type=SourceType.THREAD_REPLY,
        status=SignalStatus.CONFIRMED,
        happened_at=ref_time - timedelta(days=10),
    )

    # Event 2: Github PR review (weight = 10.0), unprocessed (*1.0), 120 days old.
    # (GITHUB, PR_REVIEW) has a 180-day half-life override: 120 days old =
    # 30 flat + 90 days decay = 0.5 half-lives -> 10.0 * 2**(-0.5) =~ 7.0711.
    ev2 = make_event(
        platform=SourcePlatform.GITHUB,
        source_type=SourceType.PR_REVIEW,
        status=SignalStatus.UNPROCESSED,
        happened_at=ref_time - timedelta(days=120),
    )

    # Event 3: Jira task (weight = 7.0), denied (*0.0) -> 0.0
    ev3 = make_event(
        platform=SourcePlatform.JIRA,
        source_type=SourceType.TASK,
        status=SignalStatus.DENIED,
    )

    # Expected sum = 4.5 + 7.0711 (rounded to 4dp by calculate_confidence_score) + 0.0
    assert calculate_confidence_score([ev1, ev2, ev3], reference_time=ref_time) == 11.5711


# ---------------------------------------------------------------------------
# Content-type-aware half-life.
# ---------------------------------------------------------------------------


def test_get_half_life_days_overrides() -> None:
    """Verify the configured override table resolves as expected."""
    assert get_half_life_days(SourcePlatform.GITHUB, SourceType.PR_REVIEW) == 180
    assert get_half_life_days(SourcePlatform.GITHUB, SourceType.COMMIT) == 180
    assert get_half_life_days(SourcePlatform.SENTRY, SourceType.INCIDENT) == 21
    assert get_half_life_days(SourcePlatform.DATADOG, SourceType.INCIDENT) == 21


def test_get_half_life_days_unmapped_combination_falls_back_to_default() -> None:
    """Any (platform, source_type) without an override falls back to
    DEFAULT_HALF_LIFE_DAYS (90), never to 0 or None.
    """
    assert DEFAULT_HALF_LIFE_DAYS == 90
    # Slack/Teams chat content -- explicitly meant to stay at the current default.
    assert get_half_life_days(SourcePlatform.SLACK, SourceType.MESSAGE) == 90
    assert get_half_life_days(SourcePlatform.MS_TEAMS, SourceType.THREAD_REPLY) == 90
    # A platform/type combination that doesn't appear in the override table at all.
    assert get_half_life_days(SourcePlatform.JIRA, SourceType.TICKET_COMMENT) == 90
    assert get_half_life_days(SourcePlatform.CUSTOM, SourceType.CUSTOM_PAYLOAD) == 90
    # Every combination actually present in the table must be a positive int,
    # not 0/None -- would zero out or crash 2 ** (-decay_days / half_life_days).
    for half_life in HALF_LIFE_DAYS_OVERRIDES.values():
        assert isinstance(half_life, int)
        assert half_life > 0


def test_confidence_decay_differs_by_source_type_for_same_event_age(make_event: Any) -> None:
    """Two contexts' events with identical age but different (platform,
    source_type) produce different confidence scores once one has a
    half-life override.
    """
    ref_time = datetime.now(UTC)
    age = timedelta(days=120)  # 30 flat + 90 days of decay

    # (GITHUB, PR_REVIEW): weight 10.0, 180-day half-life override.
    # 90 days of decay at 180-day half-life -> 10.0 * 2**(-0.5).
    ev_github_pr_review = make_event(
        platform=SourcePlatform.GITHUB, source_type=SourceType.PR_REVIEW, happened_at=ref_time - age
    )
    score_github = calculate_confidence_score([ev_github_pr_review], reference_time=ref_time)

    # (SENTRY, INCIDENT): weight 5.0 (platform base, no weight override), 21-day
    # half-life override. 90 days of decay at 21-day half-life is a much steeper
    # drop -- confirms overrides in different directions both take effect.
    ev_sentry_incident = make_event(
        platform=SourcePlatform.SENTRY, source_type=SourceType.INCIDENT, happened_at=ref_time - age
    )
    score_sentry = calculate_confidence_score([ev_sentry_incident], reference_time=ref_time)

    # (SLACK, MESSAGE): weight 2.0, no half-life override -> stays at the 90-day default.
    ev_slack_message = make_event(
        platform=SourcePlatform.SLACK, source_type=SourceType.MESSAGE, happened_at=ref_time - age
    )
    score_slack = calculate_confidence_score([ev_slack_message], reference_time=ref_time)

    expected_github = round(10.0 * 2 ** (-90 / 180.0), 4)
    expected_sentry = round(5.0 * 2 ** (-90 / 21.0), 4)
    expected_slack = round(2.0 * 2 ** (-90 / 90.0), 4)

    assert score_github == expected_github
    assert score_sentry == expected_sentry
    assert score_slack == expected_slack

    # The Sentry incident decays far more aggressively than a GitHub PR review
    # of the same age, and both differ from the un-overridden Slack default --
    # proving the per-event half-life lookup actually changes outcomes, not
    # just that the table exists.
    assert score_sentry < score_slack < score_github


def test_confidence_decay_mixed_source_types_blend_per_event(make_event: Any) -> None:
    """A context aggregating events of mixed types needs no special-casing:
    decay is computed per event before summing.
    """
    ref_time = datetime.now(UTC)
    age = timedelta(days=120)

    ev_github_pr_review = make_event(
        platform=SourcePlatform.GITHUB, source_type=SourceType.PR_REVIEW, happened_at=ref_time - age
    )
    ev_slack_message = make_event(
        platform=SourcePlatform.SLACK, source_type=SourceType.MESSAGE, happened_at=ref_time - age
    )

    combined = calculate_confidence_score(
        [ev_github_pr_review, ev_slack_message], reference_time=ref_time
    )
    # Raw (unrounded) per-event contributions, summed before rounding -- mirrors
    # calculate_confidence_score's own internal order of operations, unlike
    # summing two already-independently-rounded single-event calls would.
    expected_raw_sum = 10.0 * 2 ** (-90 / 180.0) + 2.0 * 2 ** (-90 / 90.0)

    assert combined == round(expected_raw_sum, 4)

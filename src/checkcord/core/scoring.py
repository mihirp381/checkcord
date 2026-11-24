from datetime import UTC, datetime

from checkcord.models.enums import SignalStatus, SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent

# Core Source Platform base weights
PLATFORM_WEIGHTS: dict[SourcePlatform, float] = {
    SourcePlatform.GITHUB: 8.0,
    SourcePlatform.JIRA: 7.0,
    SourcePlatform.CONFLUENCE: 6.0,
    SourcePlatform.NOTION: 5.0,
    SourcePlatform.GOOGLE_DRIVE: 4.0,
    SourcePlatform.ZOOM: 3.0,
    SourcePlatform.SENTRY: 5.0,
    SourcePlatform.DATADOG: 5.0,
    SourcePlatform.SLACK: 2.0,
    SourcePlatform.MS_TEAMS: 2.0,
    SourcePlatform.AGENT_SESSION: 1.0,
    SourcePlatform.CUSTOM: 1.0,
}

# Override weights for specific SourceType events
SOURCE_TYPE_OVERWRITES: dict[tuple[SourcePlatform, SourceType], float] = {
    (SourcePlatform.GITHUB, SourceType.PR_REVIEW): 10.0,
    (SourcePlatform.GITHUB, SourceType.COMMIT): 9.0,
    (SourcePlatform.GITHUB, SourceType.PR_OPENED): 8.0,
    (SourcePlatform.GITHUB, SourceType.PR_COMMENT): 4.0,
    (SourcePlatform.JIRA, SourceType.TASK): 7.0,
    (SourcePlatform.JIRA, SourceType.TICKET_CREATED): 6.0,
    (SourcePlatform.JIRA, SourceType.TICKET_UPDATE): 5.0,
    (SourcePlatform.JIRA, SourceType.TICKET_COMMENT): 3.0,
    (SourcePlatform.SLACK, SourceType.THREAD_REPLY): 3.0,
    (SourcePlatform.SLACK, SourceType.MESSAGE): 2.0,
    (SourcePlatform.MS_TEAMS, SourceType.THREAD_REPLY): 3.0,
    (SourcePlatform.MS_TEAMS, SourceType.MESSAGE): 2.0,
}


def get_event_weight(platform: SourcePlatform, source_type: SourceType) -> float:
    """Retrieves the configured weight for a platform/type combination, falling back gracefully."""
    # Try specific override first
    if (platform, source_type) in SOURCE_TYPE_OVERWRITES:
        return SOURCE_TYPE_OVERWRITES[(platform, source_type)]
    # Fallback to platform base weight, then default 1.0
    return PLATFORM_WEIGHTS.get(platform, 1.0)


# Confidence-decay half-life per (platform, source_type). Reviewed and
# committed decisions last longer; incident alerts fade fast. Anything
# not listed uses DEFAULT_HALF_LIFE_DAYS.
DEFAULT_HALF_LIFE_DAYS = 90
HALF_LIFE_DAYS_OVERRIDES: dict[tuple[SourcePlatform, SourceType], int] = {
    (SourcePlatform.GITHUB, SourceType.PR_REVIEW): 180,
    (SourcePlatform.GITHUB, SourceType.COMMIT): 180,
    (SourcePlatform.SENTRY, SourceType.INCIDENT): 21,
    (SourcePlatform.DATADOG, SourceType.INCIDENT): 21,
}


def get_half_life_days(platform: SourcePlatform, source_type: SourceType) -> int:
    """Decay half-life for a platform/type combination; DEFAULT_HALF_LIFE_DAYS
    when there is no override."""
    return HALF_LIFE_DAYS_OVERRIDES.get((platform, source_type), DEFAULT_HALF_LIFE_DAYS)


def calculate_confidence_score(
    events: list[CanonicalEvent], reference_time: datetime | None = None
) -> float:
    """Aggregate confidence score C(e) = sum(w_s * f_s(e)).

    - w_s: weight for the source platform and type.
    - f_s(e): signal status multiplier (CONFIRMED 1.5, DENIED 0.0, EXPIRED 0.2,
      otherwise 1.0) times a time decay factor.
    - Decay starts after 30 days: 2 ** (-age_days / half_life_days), with the
      half-life from get_half_life_days (90 days by default).
    """
    if not events:
        return 0.0

    if reference_time is None:
        reference_time = datetime.now(UTC)
    elif reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=UTC)

    total_score = 0.0

    for event in events:
        # Get base weight w_s
        w_s = get_event_weight(event.source_platform, event.source_type)

        # Signal Status multiplier
        status_mult = 1.0
        if event.signal_status == SignalStatus.CONFIRMED:
            status_mult = 1.5
        elif event.signal_status == SignalStatus.DENIED:
            status_mult = 0.0
        elif event.signal_status == SignalStatus.EXPIRED:
            status_mult = 0.2

        # Time decay factor
        happened_at = event.happened_at
        if happened_at.tzinfo is None:
            happened_at = happened_at.replace(tzinfo=UTC)

        age_seconds = (reference_time - happened_at).total_seconds()
        age_days = max(0.0, age_seconds / (24 * 3600))

        # No decay for 30 days, then exponential with the source's half-life.
        if age_days > 30:
            decay_days = age_days - 30
            half_life_days = get_half_life_days(event.source_platform, event.source_type)
            decay_factor = 2 ** (-decay_days / half_life_days)
        else:
            decay_factor = 1.0

        # Contribution of this event
        contribution = w_s * status_mult * decay_factor
        total_score += contribution

    return round(total_score, 4)

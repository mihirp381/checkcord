"""Which chat messages are bot noise."""

import re

import structlog

from checkcord.models.enums import SourcePlatform

logger = structlog.get_logger(__name__)

# A fixed list; it is not configurable yet.
BLOCKED_BOT_IDS: set[str] = {"uslackbot"}
BLOCKED_USERNAMES: set[str] = {
    "datadog",
    "jenkins",
    "sentry",
    "github",
    "jira",
    "bitbucket",
    "gitlab",
    "buildkite",
    "circleci",
    "dependabot",
    "renovate",
    "pagerduty",
    "opsgenie",
    "sonarqube",
}

# Regex to match leading '[datadog]', '[sentry]', '[jenkins]', '[github]', '[jira]', etc.
# or 'ci/cd pipeline' (case-insensitive)
BLOCKED_CONTENT_PATTERN = re.compile(
    r"^(?:\[(?:datadog|sentry|jenkins|github|jira|bitbucket|gitlab|buildkite|circleci|dependabot|renovate|pagerduty|opsgenie|sonarqube|linear)\]|ci/cd pipeline)",  # noqa: E501
    re.IGNORECASE,
)


def should_filter_message(
    platform: SourcePlatform,
    bot_id: str | None,
    username: str | None,
    text: str | None,
) -> bool:
    """Determine if a chat message should be filtered out to reduce bot noise.

    Args:
        platform: The chat platform the message originates from (Slack, MS Teams).
        bot_id: The platform-specific ID of the bot author (if applicable).
        username: The display name or username of the author.
        text: The raw or normalized message content.

    Returns:
        True if the message is identified as bot noise and should be filtered, False otherwise.
    """
    if bot_id:
        if bot_id.lower() in BLOCKED_BOT_IDS:
            logger.info(
                "chat.bot_filter.blocked_bot_id",
                platform=platform.value,
                bot_id=bot_id,
            )
            return True

    if username:
        if username.lower() in BLOCKED_USERNAMES:
            logger.info(
                "chat.bot_filter.blocked_username",
                platform=platform.value,
                username=username,
            )
            return True

    if text:
        if BLOCKED_CONTENT_PATTERN.match(text.strip()):
            logger.info(
                "chat.bot_filter.blocked_pattern",
                platform=platform.value,
                text=text,
            )
            return True

    return False

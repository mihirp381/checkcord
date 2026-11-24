"""Chat markup to Markdown, and the users, channels and groups a message mentions."""

import re

import structlog

from checkcord.models.common import EntityReference
from checkcord.models.enums import SourcePlatform

logger = structlog.get_logger(__name__)


def normalize_slack_text(text: str) -> tuple[str, list[EntityReference]]:
    """Normalize Slack proprietary formatting to clean Markdown and extract mentions.

    Replaces:
    - User mentions: <@U123> or <@U123|name> -> @U123
    - Channel mentions: <#C123> or <#C123|channel-name> -> #channel-name or #C123
    - User group mentions: <!subteam^S123> or <!subteam^S123|@group-name> -> @group-name or @S123
    - Special broadcast commands: <!here>, <!channel>, <!everyone> -> @here, @channel, @everyone
    - Links: <http://...> or <http://...|label> -> [label](http://...) or http://...

    Returns:
    - Cleaned markdown text.
    - Extracted EntityReference objects representing mentions.
    """
    if not text:
        return "", []

    entities: list[EntityReference] = []

    # Extract and replace user mentions: <@U123> or <@U123|name>
    # Regex is non-greedy and bounded to prevent ReDoS.
    user_pattern = re.compile(r"<@([A-Z0-9]{3,30})(?:\|([^>|]{1,100}))?>")

    def user_replacer(match: re.Match[str]) -> str:
        user_id = match.group(1)
        entities.append(
            EntityReference(
                entity_type="user",
                entity_id=user_id,
                entity_platform=SourcePlatform.SLACK,
                relationship="mention",
            )
        )
        # Using raw @U123 format as approved by the user to avoid downstream LLM confusion
        return f"@{user_id}"

    text_clean = user_pattern.sub(user_replacer, text)

    # Extract and replace channel mentions: <#C123> or <#C123|channel-name>
    channel_pattern = re.compile(r"<#([A-Z0-9]{3,30})(?:\|([^>|]{1,100}))?>")

    def channel_replacer(match: re.Match[str]) -> str:
        channel_id = match.group(1)
        channel_name = match.group(2)
        entities.append(
            EntityReference(
                entity_type="channel",
                entity_id=channel_id,
                entity_platform=SourcePlatform.SLACK,
                relationship="mention",
            )
        )
        return f"#{channel_name}" if channel_name else f"#{channel_id}"

    text_clean = channel_pattern.sub(channel_replacer, text_clean)

    # Extract and replace user group mentions: <!subteam^S123> or <!subteam^S123|@group-name>
    group_pattern = re.compile(r"<!subteam\^([A-Z0-9]{3,30})(?:\|([^>|]{1,100}))?>")

    def group_replacer(match: re.Match[str]) -> str:
        group_id = match.group(1)
        group_name = match.group(2)
        entities.append(
            EntityReference(
                entity_type="user_group",
                entity_id=group_id,
                entity_platform=SourcePlatform.SLACK,
                relationship="mention",
            )
        )
        if group_name:
            # Strip any leading '@' to avoid rendering as @@name
            cleaned_name = group_name.lstrip("@")
            return f"@{cleaned_name}"
        return f"@{group_id}"

    text_clean = group_pattern.sub(group_replacer, text_clean)

    # Replace special broadcast commands: <!here>, <!channel>, <!everyone>
    broadcast_pattern = re.compile(r"<!(here|channel|everyone)(?:\|([^>|]{1,100}))?>")

    def broadcast_replacer(match: re.Match[str]) -> str:
        command = match.group(1)
        label = match.group(2)
        return f"@{label}" if label else f"@{command}"

    text_clean = broadcast_pattern.sub(broadcast_replacer, text_clean)

    # Clean Slack-specific link formatting: <http://...> or <http://...|label>
    link_pattern = re.compile(r"<(https?://[^>|]{1,2000})(?:\|([^>|]{1,1000}))?>")

    def link_replacer(match: re.Match[str]) -> str:
        url = match.group(1)
        label = match.group(2)
        return f"[{label}]({url})" if label else url

    text_clean = link_pattern.sub(link_replacer, text_clean)

    return text_clean, entities


def normalize_ms_teams_text(text: str) -> tuple[str, list[EntityReference]]:
    """Normalize MS Teams HTML/proprietary formatting to clean Markdown and extract mentions.

    Replaces:
    - User mentions: <at id="8:orgid:...">Name</at> or <at>Name</at> -> @Name
    - basic HTML tags to Markdown (like <a> links, <b>, <i>, <p>)

    Returns:
    - Cleaned markdown text.
    - Extracted EntityReference objects representing mentions.
    """
    if not text:
        return "", []

    entities: list[EntityReference] = []

    # Parse user mentions: <at id="8:orgid:uuid">Name</at> or <at>Name</at>
    # Supports both standard id-based mentions and fallback empty ones.
    teams_mention_pattern = re.compile(r"<at(?: +id=\"([^\"]{1,200})\")?>([^<]{1,200})</at>")

    def teams_mention_replacer(match: re.Match[str]) -> str:
        user_id = match.group(1) or ""
        name = match.group(2)

        # If no id is provided, we can fallback to using name as the entity ID
        entity_id = user_id if user_id else name

        entities.append(
            EntityReference(
                entity_type="user",
                entity_id=entity_id,
                entity_platform=SourcePlatform.MS_TEAMS,
                relationship="mention",
            )
        )
        return f"@{name}"

    text_clean = teams_mention_pattern.sub(teams_mention_replacer, text)

    # Simple HTML link translation: <a href="url">label</a> -> [label](url)
    link_pattern = re.compile(r"<a(?: +href=\"([^\"]{1,2000})\")[^>]*>([^<]{1,1000})</a>")
    text_clean = link_pattern.sub(r"[\2](\1)", text_clean)

    # Clean remaining common HTML tags (like <p>, <br>, <b>, <i>) to Markdown
    text_clean = re.sub(r"</?p>", "\n\n", text_clean)
    text_clean = re.sub(r"<br\s*/?>", "\n", text_clean)
    text_clean = re.sub(r"</?b>", "**", text_clean)
    text_clean = re.sub(r"</?i>", "*", text_clean)

    # Strip excess whitespace/newlines
    text_clean = re.sub(r"\n{3,}", "\n\n", text_clean).strip()

    return text_clean, entities


def normalize_chat_text(platform: str, text: str) -> tuple[str, list[EntityReference]]:
    """Generic entrypoint to normalize chat message content depending on source platform.

    Degrades gracefully: if platform is not supported, logs a warning and returns raw text.
    """
    if platform == SourcePlatform.SLACK:
        return normalize_slack_text(text)
    elif platform == SourcePlatform.MS_TEAMS:
        return normalize_ms_teams_text(text)
    else:
        logger.warning("chat.normalizer.unsupported_platform", platform=platform)
        return text, []

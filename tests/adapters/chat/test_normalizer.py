"""Unit tests for the chat content normalizer module.

Tests normalization and entity extraction for Slack
and MS Teams platforms.
"""

import pytest

from checkcord.adapters.chat.core.normalizer import (
    normalize_chat_text,
    normalize_ms_teams_text,
    normalize_slack_text,
)
from checkcord.models.common import EntityReference
from checkcord.models.enums import SourcePlatform


@pytest.mark.parametrize(
    "text,expected_text,expected_entities",
    [
        # User mentions
        (
            "Hello <@U12345>",
            "Hello @U12345",
            [
                EntityReference(
                    entity_type="user",
                    entity_id="U12345",
                    entity_platform=SourcePlatform.SLACK,
                    relationship="mention",
                )
            ],
        ),
        (
            "Hello <@U12345|alice>",
            "Hello @U12345",
            [
                EntityReference(
                    entity_type="user",
                    entity_id="U12345",
                    entity_platform=SourcePlatform.SLACK,
                    relationship="mention",
                )
            ],
        ),
        # Channel mentions
        (
            "Go to <#C999>",
            "Go to #C999",
            [
                EntityReference(
                    entity_type="channel",
                    entity_id="C999",
                    entity_platform=SourcePlatform.SLACK,
                    relationship="mention",
                )
            ],
        ),
        (
            "Go to <#C999|general>",
            "Go to #general",
            [
                EntityReference(
                    entity_type="channel",
                    entity_id="C999",
                    entity_platform=SourcePlatform.SLACK,
                    relationship="mention",
                )
            ],
        ),
        # User group mentions
        (
            "Ping <!subteam^S111>",
            "Ping @S111",
            [
                EntityReference(
                    entity_type="user_group",
                    entity_id="S111",
                    entity_platform=SourcePlatform.SLACK,
                    relationship="mention",
                )
            ],
        ),
        (
            "Ping <!subteam^S111|@devs>",
            "Ping @devs",
            [
                EntityReference(
                    entity_type="user_group",
                    entity_id="S111",
                    entity_platform=SourcePlatform.SLACK,
                    relationship="mention",
                )
            ],
        ),
        (
            "Ping <!subteam^S111|devs>",
            "Ping @devs",
            [
                EntityReference(
                    entity_type="user_group",
                    entity_id="S111",
                    entity_platform=SourcePlatform.SLACK,
                    relationship="mention",
                )
            ],
        ),
        # Special broadcasts
        ("Hey <!here>!", "Hey @here!", []),
        ("Attention <!channel|everyone>!", "Attention @everyone!", []),
        # Links
        (
            "Check <https://google.com>",
            "Check https://google.com",
            [],
        ),
        (
            "Check <https://google.com|Google>",
            "Check [Google](https://google.com)",
            [],
        ),
        # Empty string
        ("", "", []),
    ],
)
def test_normalize_slack_text(
    text: str, expected_text: str, expected_entities: list[EntityReference]
) -> None:
    """Test normalization and entity extraction for Slack inputs."""
    clean_text, entities = normalize_slack_text(text)
    assert clean_text == expected_text
    assert entities == expected_entities


def test_normalize_slack_complex() -> None:
    """Test Slack normalization with multiple entities and links in one message."""
    text = (
        "Hey <@U123|bob> and <@U456>, check <#C999|general> and <https://example.com|this site>! "
        "Also pinging group <!subteam^S789|ops-team>."
    )
    expected_text = (
        "Hey @U123 and @U456, check #general and [this site](https://example.com)! "
        "Also pinging group @ops-team."
    )

    clean_text, entities = normalize_slack_text(text)
    assert clean_text == expected_text
    assert len(entities) == 4

    assert entities[0] == EntityReference(
        entity_type="user",
        entity_id="U123",
        entity_platform=SourcePlatform.SLACK,
        relationship="mention",
    )
    assert entities[1] == EntityReference(
        entity_type="user",
        entity_id="U456",
        entity_platform=SourcePlatform.SLACK,
        relationship="mention",
    )
    assert entities[2] == EntityReference(
        entity_type="channel",
        entity_id="C999",
        entity_platform=SourcePlatform.SLACK,
        relationship="mention",
    )
    assert entities[3] == EntityReference(
        entity_type="user_group",
        entity_id="S789",
        entity_platform=SourcePlatform.SLACK,
        relationship="mention",
    )


@pytest.mark.parametrize(
    "text,expected_text,expected_entities",
    [
        # User mentions with ID
        (
            'Hello <at id="8:orgid:a7b3e64f-4d37-4d94-a15d-3197775988f6">Alice</at>',
            "Hello @Alice",
            [
                EntityReference(
                    entity_type="user",
                    entity_id="8:orgid:a7b3e64f-4d37-4d94-a15d-3197775988f6",
                    entity_platform=SourcePlatform.MS_TEAMS,
                    relationship="mention",
                )
            ],
        ),
        # User mentions without ID (fallback to name)
        (
            "Hello <at>Bob Smith</at>",
            "Hello @Bob Smith",
            [
                EntityReference(
                    entity_type="user",
                    entity_id="Bob Smith",
                    entity_platform=SourcePlatform.MS_TEAMS,
                    relationship="mention",
                )
            ],
        ),
        # HTML links
        (
            'Check <a href="https://example.com">Example Site</a>',
            "Check [Example Site](https://example.com)",
            [],
        ),
        # HTML paragraph and break formatting tags
        (
            "<p>First paragraph.</p><p>Second paragraph.<br/>With break.</p>",
            "First paragraph.\n\nSecond paragraph.\nWith break.",
            [],
        ),
        # Bold and italic formatting tags
        (
            "This is <b>bold</b> and <i>italic</i> text.",
            "This is **bold** and *italic* text.",
            [],
        ),
    ],
)
def test_normalize_ms_teams_text(
    text: str, expected_text: str, expected_entities: list[EntityReference]
) -> None:
    """Test normalization and entity extraction for Microsoft Teams HTML inputs."""
    clean_text, entities = normalize_ms_teams_text(text)
    assert clean_text == expected_text
    assert entities == expected_entities


def test_normalize_chat_text_routing() -> None:
    """Test the generic entrypoint routing to correct platform parser."""
    text_slack = "Hello <@U123>"
    text_teams = "Hello <at>Bob</at>"
    text_unknown = "Hello @Bob"

    # Slack routing
    clean_s, ent_s = normalize_chat_text(SourcePlatform.SLACK, text_slack)
    assert clean_s == "Hello @U123"
    assert len(ent_s) == 1

    # Teams routing
    clean_t, ent_t = normalize_chat_text(SourcePlatform.MS_TEAMS, text_teams)
    assert clean_t == "Hello @Bob"
    assert len(ent_t) == 1

    # Unsupported platform routing (graceful degradation)
    clean_u, ent_u = normalize_chat_text(SourcePlatform.GITHUB, text_unknown)
    assert clean_u == text_unknown
    assert ent_u == []


def test_malformed_syntax_graceful_degradation() -> None:
    """Test that malformed/unclosed tags degrade gracefully without raising exceptions."""
    # Slack unclosed tags
    slack_malformed = "Hello <@U123 and channel <#C456|general"
    clean_s, ent_s = normalize_slack_text(slack_malformed)
    assert clean_s == slack_malformed
    assert ent_s == []

    # Teams unclosed tags
    teams_malformed = 'Hello <at id="123">Bob'
    clean_t, ent_t = normalize_ms_teams_text(teams_malformed)
    assert clean_t == teams_malformed
    assert ent_t == []

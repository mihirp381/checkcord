"""Unit tests for core bot noise filtering logic."""

from checkcord.adapters.chat.core.bot_filter import should_filter_message
from checkcord.models.enums import SourcePlatform


def test_should_filter_message_bot_id() -> None:
    """Verify messages with blocked bot_id are filtered."""
    assert should_filter_message(SourcePlatform.SLACK, "USLACKBOT", "user", "text") is True
    assert should_filter_message(SourcePlatform.SLACK, "uslackbot", "user", "text") is True
    assert should_filter_message(SourcePlatform.SLACK, "other_bot", "user", "text") is False


def test_should_filter_message_username() -> None:
    """Verify messages with blocked username are filtered."""
    assert should_filter_message(SourcePlatform.SLACK, None, "datadog", "text") is True
    assert should_filter_message(SourcePlatform.SLACK, None, "Datadog", "text") is True
    assert should_filter_message(SourcePlatform.SLACK, None, "jenkins", "text") is True
    assert should_filter_message(SourcePlatform.SLACK, None, "sentry", "text") is True
    assert should_filter_message(SourcePlatform.SLACK, None, "github", "text") is True
    assert should_filter_message(SourcePlatform.SLACK, None, "jira", "text") is True
    assert should_filter_message(SourcePlatform.SLACK, None, "bitbucket", "text") is True
    assert should_filter_message(SourcePlatform.SLACK, None, "gitlab", "text") is True
    assert should_filter_message(SourcePlatform.SLACK, None, "buildkite", "text") is True
    assert should_filter_message(SourcePlatform.SLACK, None, "circleci", "text") is True
    assert should_filter_message(SourcePlatform.SLACK, None, "normal_user", "text") is False


def test_should_filter_message_text_patterns() -> None:
    """Verify messages matching blocked text patterns are filtered."""
    # Lead-in datadog
    assert should_filter_message(SourcePlatform.SLACK, None, "user", "[datadog] alert") is True
    assert should_filter_message(SourcePlatform.SLACK, None, "user", "   [datadog] alert") is True
    assert should_filter_message(SourcePlatform.SLACK, None, "user", "[DATADOG] alert") is True

    # Lead-in sentry
    assert should_filter_message(SourcePlatform.SLACK, None, "user", "[sentry] error") is True

    # Lead-in jenkins
    assert should_filter_message(SourcePlatform.SLACK, None, "user", "[jenkins] build #4") is True

    # Lead-in github / pagerduty / dependabot
    assert (
        should_filter_message(SourcePlatform.SLACK, None, "user", "[github] PR #123 merged") is True
    )
    assert (
        should_filter_message(SourcePlatform.SLACK, None, "user", "[pagerduty] Alert #42") is True
    )
    assert (
        should_filter_message(SourcePlatform.SLACK, None, "user", "[dependabot] Security bump")
        is True
    )

    # Lead-in ci/cd pipeline
    assert (
        should_filter_message(SourcePlatform.SLACK, None, "user", "ci/cd pipeline started") is True
    )
    assert (
        should_filter_message(SourcePlatform.SLACK, None, "user", "CI/CD Pipeline failed") is True
    )

    # Pattern not at start or different text
    assert should_filter_message(SourcePlatform.SLACK, None, "user", "Hello [datadog]") is False
    assert (
        should_filter_message(SourcePlatform.SLACK, None, "user", "This is a ci/cd pipeline")
        is False
    )
    assert should_filter_message(SourcePlatform.SLACK, None, "user", "normal text") is False

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from checkcord.models import (
    AgentMetadata,
    CanonicalEvent,
    ChatMetadata,
    CodeMetadata,
    IncidentMetadata,
    MeetingMetadata,
    SourcePlatform,
    SourceType,
)


@pytest.fixture
def valid_event_data() -> dict[str, str | int | dict[str, str | bool] | datetime]:
    return {
        "tenant_id": str(uuid4()),
        "source_platform": SourcePlatform.SLACK,
        "source_type": SourceType.MESSAGE,
        "source_id": "msg_123",
        "happened_at": datetime(2023, 1, 1, 12, 0, tzinfo=UTC),
        "author": {"source_id": "user_456", "display_name": "Test User"},
        "content_raw": '{"text": "hello"}',
        "content_clean": "hello",
        "access_control": {"is_public": False},
    }


def test_canonical_event_valid_creation(valid_event_data: dict[str, Any]) -> None:
    """Test successful creation and default generation."""
    event = CanonicalEvent(**valid_event_data)

    assert event.event_id is not None
    assert event.ingested_at is not None
    assert event.fingerprint != ""
    assert event.version == 1


def test_fingerprint_is_deterministic(valid_event_data: dict[str, Any]) -> None:
    """Test that identical inputs produce the exact same fingerprint hash."""
    event1 = CanonicalEvent(**valid_event_data)
    event2 = CanonicalEvent(**valid_event_data)

    assert event1.fingerprint == event2.fingerprint


def test_fingerprint_changes_on_version(valid_event_data: dict[str, Any]) -> None:
    """Test that a new version produces a new fingerprint."""
    event1 = CanonicalEvent(**valid_event_data)

    data2 = dict(valid_event_data)
    data2["version"] = 2
    event2 = CanonicalEvent(**data2)

    assert event1.fingerprint != event2.fingerprint


def test_timezone_enforcement(valid_event_data: dict[str, Any]) -> None:
    """Test that naive datetimes are rejected."""
    data = dict(valid_event_data)
    data["happened_at"] = datetime(2023, 1, 1, 12, 0)  # Naive

    with pytest.raises(ValueError, match="timezone-aware"):
        CanonicalEvent(**data)


def test_immutability(valid_event_data: dict[str, Any]) -> None:
    """Test that FrozenModel prevents mutation."""
    event = CanonicalEvent(**valid_event_data)

    with pytest.raises(ValidationError):
        event.version = 2


def test_to_redis_dict(valid_event_data: dict[str, Any]) -> None:
    """Test Redis serialization format (flat strings)."""
    event = CanonicalEvent(**valid_event_data)
    redis_dict = event.to_redis_dict()

    assert isinstance(redis_dict, dict)
    assert redis_dict["source_id"] == "msg_123"

    # Nested objects should be JSON strings
    assert isinstance(redis_dict["author"], str)
    author_dict = json.loads(redis_dict["author"])
    assert author_dict["source_id"] == "user_456"


def test_to_postgres_dict(valid_event_data: dict[str, Any]) -> None:
    """Test Postgres serialization format (native types + dicts)."""
    event = CanonicalEvent(**valid_event_data)
    pg_dict = event.to_postgres_dict()

    # UUIDs and datetimes should remain objects
    assert pg_dict["tenant_id"] == event.tenant_id
    assert pg_dict["happened_at"] == event.happened_at

    # Nested models should become dicts
    assert isinstance(pg_dict["author"], dict)
    assert pg_dict["author"]["source_id"] == "user_456"


def test_redis_round_trip(valid_event_data: dict[str, Any]) -> None:
    """Test serializing to Redis and re-hydrating produces an identical event."""
    event1 = CanonicalEvent(**valid_event_data)
    redis_dict = event1.to_redis_dict()
    event2 = CanonicalEvent.from_redis_dict(redis_dict)

    assert event1 == event2


def test_meeting_metadata_duration_constraint() -> None:
    """Test MeetingMetadata enforces completed meeting duration (gt=0).

    Ongoing/active meetings fail validation.
    """
    valid_meeting = MeetingMetadata(duration_seconds=300, attendee_count=4)
    assert valid_meeting.duration_seconds == 300

    # Ongoing/active meeting attempt with 0 or negative duration must be rejected
    with pytest.raises(ValidationError):
        MeetingMetadata(duration_seconds=0)

    with pytest.raises(ValidationError):
        MeetingMetadata(duration_seconds=-10)


def test_enriched_metadata_serialization_round_trip(valid_event_data: dict[str, Any]) -> None:
    """Test fully populated event with enriched metadata models."""
    data = dict(valid_event_data)
    data["code_metadata"] = CodeMetadata(
        repository="org/repo",
        branch="main",
        files_changed=["src/main.py"],
        primary_language="Python",
        is_rollback=True,
    )
    data["agent_metadata"] = AgentMetadata(
        session_id="sess_789",
        model_name="claude-3-5-sonnet",
        execution_time_ms=1250,
    )
    data["incident_metadata"] = IncidentMetadata(
        severity="SEV-1",
        environment="production",
        affected_services=["auth-service", "api-gateway"],
        root_cause_identified=True,
    )
    data["meeting_metadata"] = MeetingMetadata(
        duration_seconds=1800,
        attendee_count=5,
        meeting_type="architecture_review",
        has_recording=True,
    )
    data["chat_metadata"] = ChatMetadata(
        channel_id="C123456",
        thread_id="T987654",
        reply_count=12,
    )

    event1 = CanonicalEvent(**data)

    # Test getters
    assert event1.code_metadata.primary_language == "Python"
    assert event1.code_metadata.is_rollback is True
    assert event1.agent_metadata.model_name == "claude-3-5-sonnet"
    assert event1.agent_metadata.execution_time_ms == 1250
    assert event1.incident_metadata.severity == "SEV-1"
    assert event1.meeting_metadata.duration_seconds == 1800
    assert event1.chat_metadata.reply_count == 12

    # Redis round-trip
    redis_dict = event1.to_redis_dict()
    event2 = CanonicalEvent.from_redis_dict(redis_dict)

    assert event1 == event2
    assert event2.code_metadata.primary_language == "Python"
    assert event2.incident_metadata.environment == "production"
    assert event2.chat_metadata.reply_count == 12

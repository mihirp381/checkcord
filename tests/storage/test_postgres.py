"""Tests for PostgresEventStore – unit + integration."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.storage.postgres import PostgresEventStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant_id() -> Any:
    return uuid4()


@pytest.fixture
def make_event(tenant_id: Any) -> Any:
    """Factory that returns a fresh CanonicalEvent per call."""

    def _make(**overrides: Any) -> CanonicalEvent:
        defaults: dict[str, Any] = {
            "tenant_id": tenant_id,
            "source_platform": SourcePlatform.SLACK,
            "source_type": SourceType.MESSAGE,
            "source_id": f"msg_{uuid4().hex[:8]}",
            "happened_at": datetime(2026, 6, 20, 12, 0, tzinfo=UTC),
            "author": {"source_id": "user_456"},
            "content_raw": '{"text": "hello"}',
            "content_clean": "hello",
            "access_control": {
                "is_public": False,
                "allowed_identity_ids": [str(uuid4()), str(uuid4())],
            },
        }
        defaults.update(overrides)
        return CanonicalEvent(**defaults)

    return _make


# ---------------------------------------------------------------------------
# Unit tests – _to_insert_dict  (no database needed)
# ---------------------------------------------------------------------------


class TestToInsertDict:
    """Validate the column-extraction logic for GIN-indexed arrays."""

    def test_access_allowed_ids_extracted(self, make_event: Any) -> None:
        """allowed_identity_ids must be popped out to top-level."""
        event = make_event()
        row = PostgresEventStore._to_insert_dict(event)

        assert "access_allowed_ids" in row
        assert isinstance(row["access_allowed_ids"], list)
        assert len(row["access_allowed_ids"]) == 2

        # Must be removed from the JSONB blob
        ac = row["access_control"]
        assert "allowed_identity_ids" not in ac

    def test_code_files_changed_extracted_when_present(self, make_event: Any) -> None:
        """files_changed must be popped to top-level when code_metadata exists."""
        event = make_event(
            code_metadata={
                "repository": "acme/api",
                "branch": "main",
                "files_changed": ["src/app.py", "tests/test_app.py"],
            }
        )
        row = PostgresEventStore._to_insert_dict(event)

        assert row["code_files_changed"] == ["src/app.py", "tests/test_app.py"]

        # Must be removed from the JSONB blob
        cm = row["code_metadata"]
        assert "files_changed" not in cm

    def test_code_files_changed_defaults_empty_when_no_metadata(self, make_event: Any) -> None:
        """When code_metadata is None, code_files_changed must be []."""
        event = make_event(code_metadata=None)
        row = PostgresEventStore._to_insert_dict(event)

        assert row["code_files_changed"] == []

    def test_str_enum_values_are_plain_strings(self, make_event: Any) -> None:
        """StrEnum fields must be cast to plain str for non-JSONB columns."""
        event = make_event()
        row = PostgresEventStore._to_insert_dict(event)

        assert row["source_platform"] == "slack"
        assert row["source_type"] == "message"
        assert row["signal_status"] == "unprocessed"
        assert isinstance(row["source_platform"], str)

    def test_insert_dict_has_all_required_columns(self, make_event: Any) -> None:
        """Smoke test: every column the table expects must be present."""
        event = make_event()
        row = PostgresEventStore._to_insert_dict(event)

        required_keys = {
            "event_id",
            "tenant_id",
            "fingerprint",
            "source_platform",
            "source_type",
            "event_action",
            "source_id",
            "version",
            "happened_at",
            "ingested_at",
            "content_raw",
            "content_clean",
            "signal_status",
            "author",
            "access_control",
            "source_raw_payload",
            "entities",
            "metadata",
            "access_allowed_ids",
            "code_files_changed",
            "agent_metadata",
            "incident_metadata",
            "meeting_metadata",
            "chat_metadata",
        }
        assert required_keys.issubset(row.keys())


# ---------------------------------------------------------------------------
# Unit tests – _row_to_event  (no database needed)
# ---------------------------------------------------------------------------


class TestRowToEvent:
    """Validate the reverse merge for read path."""

    def test_round_trip(self, make_event: Any) -> None:
        """insert-dict → row-to-event must reconstruct the original event."""
        original = make_event()
        insert_dict = PostgresEventStore._to_insert_dict(original)

        # Simulate what Postgres would return: the insert_dict IS the row
        reconstructed = PostgresEventStore._row_to_event(dict(insert_dict))

        assert reconstructed.event_id == original.event_id
        assert reconstructed.tenant_id == original.tenant_id
        assert reconstructed.fingerprint == original.fingerprint
        assert reconstructed.source_platform == original.source_platform
        assert (
            reconstructed.access_control.allowed_identity_ids
            == original.access_control.allowed_identity_ids
        )

    def test_round_trip_with_code_metadata(self, make_event: Any) -> None:
        """Ensure code_metadata.files_changed survives the round trip."""
        original = make_event(
            code_metadata={
                "repository": "acme/api",
                "branch": "main",
                "files_changed": ["README.md"],
            }
        )
        insert_dict = PostgresEventStore._to_insert_dict(original)
        reconstructed = PostgresEventStore._row_to_event(dict(insert_dict))

        assert reconstructed.code_metadata is not None
        assert reconstructed.code_metadata.files_changed == ["README.md"]

    def test_round_trip_with_new_metadata_domains(self, make_event: Any) -> None:
        """Ensure incident_metadata, meeting_metadata, and chat_metadata survive the round trip."""
        original = make_event(
            incident_metadata={
                "severity": "SEV-1",
                "environment": "production",
                "affected_services": ["payment-service"],
            },
            meeting_metadata={
                "duration_seconds": 1800,
                "attendee_count": 5,
                "has_recording": True,
            },
            chat_metadata={
                "channel_id": "C12345",
                "thread_id": "T67890",
                "reply_count": 3,
            },
        )
        insert_dict = PostgresEventStore._to_insert_dict(original)
        reconstructed = PostgresEventStore._row_to_event(dict(insert_dict))

        assert reconstructed.incident_metadata is not None
        assert reconstructed.incident_metadata.severity == "SEV-1"
        assert reconstructed.incident_metadata.affected_services == ["payment-service"]

        assert reconstructed.meeting_metadata is not None
        assert reconstructed.meeting_metadata.duration_seconds == 1800
        assert reconstructed.meeting_metadata.has_recording is True

        assert reconstructed.chat_metadata is not None
        assert reconstructed.chat_metadata.channel_id == "C12345"
        assert reconstructed.chat_metadata.reply_count == 3

    def test_round_trip_without_code_metadata(self, make_event: Any) -> None:
        """When code_metadata is None, reconstruction must not crash."""
        original = make_event(code_metadata=None)
        insert_dict = PostgresEventStore._to_insert_dict(original)
        reconstructed = PostgresEventStore._row_to_event(dict(insert_dict))

        assert reconstructed.code_metadata is None

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from checkcord.core.exceptions import LinkageAdjudicationError
from checkcord.core.redaction import StoredRedaction
from checkcord.llm.client import LLMClient
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline.linkage import (
    LinkageWorker,
    closest,
    cut_excerpt,
    extract_identifiers,
    is_restatement,
    joinable,
    update_excerpt,
)
from checkcord.storage.postgres import PostgresEventStore

pytestmark = pytest.mark.asyncio


_STORED = StoredRedaction("stored redacted text", "1:test")


@pytest.fixture(autouse=True)
def stored_redaction() -> Any:
    with patch(
        "checkcord.pipeline.linkage.redact_for_storage", AsyncMock(return_value=_STORED)
    ) as redact:
        yield redact


class _Public(dict[Any, Any]):
    def get(self, key: Any, default: Any = None) -> Any:
        return {"is_public": True}


@pytest.fixture(autouse=True)
def public_audiences() -> Any:
    with patch(
        "checkcord.pipeline.linkage._audiences",
        AsyncMock(return_value=({"is_public": True}, _Public())),
    ) as found:
        yield found


def _private(*ids: Any) -> dict[str, Any]:
    return {"is_public": False, "allowed_identity_ids": [str(i) for i in ids]}


@pytest.mark.parametrize(
    ("event", "record", "kept"),
    [
        ({"is_public": True}, {"is_public": True}, True),
        (_private("a"), {"is_public": True}, True),
        ({"is_public": True}, _private("a"), False),
        (_private("a"), _private("a", "b"), True),
        (_private("a", "c"), _private("a", "b"), False),
        (_private(), _private("a"), True),
    ],
)
async def test_an_event_joins_only_a_record_its_readers_can_read(
    event: dict[str, Any], record: dict[str, Any], kept: bool
) -> None:
    record_id = uuid4()
    matches = [{"context_id": str(record_id)}]
    assert bool(joinable(event, {record_id: record}, matches)) is kept


@pytest.fixture
def tenant_id() -> Any:
    return uuid4()


@pytest.fixture
def make_event(tenant_id: Any) -> Any:
    def _make(**overrides: Any) -> CanonicalEvent:
        defaults = {
            "tenant_id": tenant_id,
            "source_platform": SourcePlatform.SLACK,
            "source_type": SourceType.MESSAGE,
            "source_id": "msg_123",
            "happened_at": "2026-06-20T12:00:00Z",
            "author": {"source_id": "user_456"},
            "content_raw": "hello",
            "content_clean": "hello",
            "access_control": {"is_public": True},
        }
        defaults.update(overrides)
        return CanonicalEvent(**defaults)

    return _make


async def test_extract_identifiers() -> None:
    # Test JIRA ticket extraction
    assert extract_identifiers("This is a fix for JIRA-1234") == {"JIRA-1234"}
    assert extract_identifiers("PROJ-456: add feature") == {"PROJ-456"}
    assert extract_identifiers("Fix PR-105 issue") == {"PR-105"}
    assert extract_identifiers("Fix PR#105 issue") == {"PR-105"}
    assert extract_identifiers("Fix #105 issue") == {"PR-105"}
    assert extract_identifiers("JIRA-123 and PR-456") == {"JIRA-123", "PR-456"}
    assert extract_identifiers("No identifiers here") == set()


async def test_closest_prefers_a_match_naming_the_same_code(make_event: Any) -> None:
    event = make_event(content_clean="This supersedes rebuild_rate_table; use FetchQuote")
    first = {"context_id": "a", "summary": "Money is integer cents"}
    named = {"context_id": "b", "summary": "rebuild_rate_table runs nightly"}
    assert closest(event, [first, named]) is named
    assert closest(make_event(content_clean="no names"), [first, named]) is first


async def test_linkage_worker_deterministic_match(make_event: Any, tenant_id: Any) -> None:
    # Arrange
    event = make_event(
        content_clean="Implementing router for JIRA-123",
        metadata={
            "clustering_matches": [
                {
                    "context_id": str(uuid4()),
                    "summary": "This is related to JIRA-123 decision",
                    "confidence_score": 0.5,
                    "similarity": 0.92,
                }
            ]
        },
    )
    mock_bus = AsyncMock()
    mock_engine = MagicMock()
    mock_llm = AsyncMock(spec=LLMClient)

    mock_store = AsyncMock(spec=PostgresEventStore)
    mock_store.get_event_hold.return_value = (True, None)
    mock_store.get_record_holds.side_effect = lambda _tenant, ids: dict.fromkeys(ids)

    with (
        patch("checkcord.pipeline.linkage.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.linkage.PostgresEventStore", return_value=mock_store),
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = MagicMock()
        mock_tenant_context.return_value = mock_ctx

        worker = LinkageWorker(bus=mock_bus, engine=mock_engine, llm_client=mock_llm)

        # Act
        await worker.process_message(event)

        # Assert
        # LLM should NOT be called because deterministic matches run first
        mock_llm.judge_linkage.assert_not_called()
        # link_context_to_event should be called
        mock_store.link_context_to_event.assert_called_once()
        mock_store.save_canonical_context.assert_not_called()


async def test_linkage_worker_llm_match(make_event: Any, tenant_id: Any) -> None:
    # Arrange
    ctx_id = uuid4()
    event = make_event(
        content_clean="Similar topic but no exact jira id",
        metadata={
            "clustering_matches": [
                {
                    "context_id": str(ctx_id),
                    "summary": "Context summary about topics",
                    "confidence_score": 0.5,
                    "similarity": 0.90,
                }
            ]
        },
    )
    mock_bus = AsyncMock()
    mock_engine = MagicMock()
    mock_llm = AsyncMock(spec=LLMClient)
    mock_llm.judge_linkage.return_value = "same"

    mock_store = AsyncMock(spec=PostgresEventStore)
    mock_store.get_event_hold.return_value = (True, None)
    mock_store.get_record_holds.side_effect = lambda _tenant, ids: dict.fromkeys(ids)

    with (
        patch("checkcord.pipeline.linkage.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.linkage.PostgresEventStore", return_value=mock_store),
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = MagicMock()
        mock_tenant_context.return_value = mock_ctx

        worker = LinkageWorker(bus=mock_bus, engine=mock_engine, llm_client=mock_llm)

        # Act
        await worker.process_message(event)

        # Assert
        mock_llm.judge_linkage.assert_called_once()
        mock_store.link_context_to_event.assert_called_once_with(
            tenant_id=tenant_id,
            canonical_id=ctx_id,
            event_id=event.event_id,
            excerpt=_STORED,
        )
        mock_store.save_canonical_context.assert_not_called()


async def test_linkage_worker_no_match_creates_context(make_event: Any, tenant_id: Any) -> None:
    # Arrange
    event = make_event(
        content_clean="Totally unique design discussion",
        metadata={
            "embedding": [0.1] * 1536,
            "clustering_matches": [
                {
                    "context_id": str(uuid4()),
                    "summary": "Some other design context",
                    "confidence_score": 0.3,
                    "similarity": 0.88,
                }
            ],
        },
    )
    mock_bus = AsyncMock()
    mock_engine = MagicMock()
    mock_llm = AsyncMock(spec=LLMClient)
    mock_llm.judge_linkage.return_value = "different"

    mock_store = AsyncMock(spec=PostgresEventStore)
    mock_store.get_event_hold.return_value = (True, None)
    mock_store.get_record_holds.side_effect = lambda _tenant, ids: dict.fromkeys(ids)
    new_ctx_id = uuid4()
    mock_store.save_canonical_context.return_value = new_ctx_id

    with (
        patch("checkcord.pipeline.linkage.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.linkage.PostgresEventStore", return_value=mock_store),
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = MagicMock()
        mock_tenant_context.return_value = mock_ctx

        worker = LinkageWorker(bus=mock_bus, engine=mock_engine, llm_client=mock_llm)

        # Act
        await worker.process_message(event)

        # Assert
        mock_llm.judge_linkage.assert_called_once()
        mock_store.save_canonical_context.assert_called_once_with(
            tenant_id=tenant_id,
            summary=event.content_clean,
            confidence_score=0.0,
            embedding=[0.1] * 1536,
            access_control=event.access_control.model_dump(mode="python"),
            scope_repo_identifier=None,
            context_type=None,
            base_event_id=event.event_id,
            held_connection=None,
            redaction=_STORED,
        )
        mock_store.link_context_to_event.assert_called_once_with(
            tenant_id=tenant_id,
            canonical_id=new_ctx_id,
            event_id=event.event_id,
        )


async def test_linkage_worker_bootstrap_scopes_to_code_metadata_repo(
    make_event: Any, tenant_id: Any
) -> None:
    """A bootstrapped context is scoped to its originating event's repo."""
    # Arrange
    event = make_event(
        content_clean="A code-linked design discussion",
        code_metadata={"repository": "acme/service-a", "branch": "main"},
        metadata={
            "embedding": [0.4] * 1536,
            "clustering_matches": [
                {
                    "context_id": str(uuid4()),
                    "summary": "Some other design context",
                    "confidence_score": 0.3,
                    "similarity": 0.88,
                }
            ],
        },
    )
    mock_bus = AsyncMock()
    mock_engine = MagicMock()
    mock_llm = AsyncMock(spec=LLMClient)
    mock_llm.judge_linkage.return_value = "different"

    mock_store = AsyncMock(spec=PostgresEventStore)
    mock_store.get_event_hold.return_value = (True, None)
    mock_store.get_record_holds.side_effect = lambda _tenant, ids: dict.fromkeys(ids)
    new_ctx_id = uuid4()
    mock_store.save_canonical_context.return_value = new_ctx_id

    with (
        patch("checkcord.pipeline.linkage.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.linkage.PostgresEventStore", return_value=mock_store),
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = MagicMock()
        mock_tenant_context.return_value = mock_ctx

        worker = LinkageWorker(bus=mock_bus, engine=mock_engine, llm_client=mock_llm)

        # Act
        await worker.process_message(event)

        # Assert
        mock_store.save_canonical_context.assert_called_once_with(
            tenant_id=tenant_id,
            summary=event.content_clean,
            confidence_score=0.0,
            embedding=[0.4] * 1536,
            access_control=event.access_control.model_dump(mode="python"),
            scope_repo_identifier="acme/service-a",
            context_type=None,
            base_event_id=event.event_id,
            held_connection=None,
            redaction=_STORED,
        )


async def test_linkage_worker_bootstrap_propagates_context_type(
    make_event: Any, tenant_id: Any
) -> None:
    """A bootstrapped context's context_type is read from
    event.metadata['context_type'], which
    publish_extraction_as_context_candidate sets for a verified
    ADR/Constraint/Risk extraction.
    """
    event = make_event(
        content_clean="Decision: adopt Postgres for event storage.",
        metadata={
            "embedding": [0.5] * 1536,
            "context_type": "adr",
            "clustering_matches": [],
        },
    )
    mock_bus = AsyncMock()
    mock_engine = MagicMock()
    mock_llm = AsyncMock(spec=LLMClient)

    mock_store = AsyncMock(spec=PostgresEventStore)
    mock_store.get_event_hold.return_value = (True, None)
    mock_store.get_record_holds.side_effect = lambda _tenant, ids: dict.fromkeys(ids)
    new_ctx_id = uuid4()
    mock_store.save_canonical_context.return_value = new_ctx_id

    with (
        patch("checkcord.pipeline.linkage.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.linkage.PostgresEventStore", return_value=mock_store),
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = MagicMock()
        mock_tenant_context.return_value = mock_ctx

        worker = LinkageWorker(bus=mock_bus, engine=mock_engine, llm_client=mock_llm)

        await worker.process_message(event)

        mock_llm.judge_linkage.assert_not_called()
        mock_store.save_canonical_context.assert_called_once_with(
            tenant_id=tenant_id,
            summary=event.content_clean,
            confidence_score=0.0,
            embedding=[0.5] * 1536,
            access_control=event.access_control.model_dump(mode="python"),
            scope_repo_identifier=None,
            context_type="adr",
            base_event_id=event.event_id,
            held_connection=None,
            redaction=_STORED,
        )


async def test_linkage_worker_llm_timeout_does_not_fail_open(
    make_event: Any, tenant_id: Any
) -> None:
    """On LLM timeout, the worker must NOT bootstrap a duplicate canonical context.

    It must raise so the event bus's retry/DLQ machinery handles it instead of
    silently minting a duplicate (see LinkageAdjudicationError).
    """
    # Arrange
    event = make_event(
        content_clean="Slow LLM check",
        metadata={
            "embedding": [0.2] * 1536,
            "clustering_matches": [
                {
                    "context_id": str(uuid4()),
                    "summary": "Existing Context",
                    "confidence_score": 0.6,
                    "similarity": 0.90,
                }
            ],
        },
    )
    mock_bus = AsyncMock()
    mock_engine = MagicMock()

    # LLM mock that sleeps longer than the timeout
    async def slow_validate(*args, **kwargs):
        await asyncio.sleep(0.5)
        return "same"

    mock_llm = AsyncMock(spec=LLMClient)
    mock_llm.judge_linkage.side_effect = slow_validate

    mock_store = AsyncMock(spec=PostgresEventStore)
    mock_store.get_event_hold.return_value = (True, None)
    mock_store.get_record_holds.side_effect = lambda _tenant, ids: dict.fromkeys(ids)
    new_ctx_id = uuid4()
    mock_store.save_canonical_context.return_value = new_ctx_id

    with (
        patch("checkcord.pipeline.linkage.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.linkage.PostgresEventStore", return_value=mock_store),
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = MagicMock()
        mock_tenant_context.return_value = mock_ctx

        # Set llm_timeout_seconds to a very small value (e.g. 0.05) to trigger timeout
        worker = LinkageWorker(
            bus=mock_bus,
            engine=mock_engine,
            llm_client=mock_llm,
            llm_timeout_seconds=0.05,
        )

        # Act / Assert
        with pytest.raises(LinkageAdjudicationError, match=r"no verdict within 0\.05s"):
            await worker.process_message(event)

        # Must never bootstrap a new (duplicate) canonical context on a timeout
        mock_store.save_canonical_context.assert_not_called()
        mock_store.link_context_to_event.assert_not_called()


async def test_linkage_worker_llm_exception_does_not_fail_open(
    make_event: Any, tenant_id: Any
) -> None:
    """Same guarantee as the timeout case, but for a generic LLM exception."""
    # Arrange
    event = make_event(
        content_clean="LLM call blows up",
        metadata={
            "embedding": [0.2] * 1536,
            "clustering_matches": [
                {
                    "context_id": str(uuid4()),
                    "summary": "Existing Context",
                    "confidence_score": 0.6,
                    "similarity": 0.90,
                }
            ],
        },
    )
    mock_bus = AsyncMock()
    mock_engine = MagicMock()

    mock_llm = AsyncMock(spec=LLMClient)
    mock_llm.judge_linkage.side_effect = RuntimeError("upstream LLM API error")

    mock_store = AsyncMock(spec=PostgresEventStore)
    mock_store.get_event_hold.return_value = (True, None)
    mock_store.get_record_holds.side_effect = lambda _tenant, ids: dict.fromkeys(ids)

    with (
        patch("checkcord.pipeline.linkage.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.linkage.PostgresEventStore", return_value=mock_store),
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = MagicMock()
        mock_tenant_context.return_value = mock_ctx

        worker = LinkageWorker(bus=mock_bus, engine=mock_engine, llm_client=mock_llm)

        # Act / Assert
        with pytest.raises(LinkageAdjudicationError, match="call failed: upstream LLM API error"):
            await worker.process_message(event)

        mock_store.save_canonical_context.assert_not_called()
        mock_store.link_context_to_event.assert_not_called()


async def test_linkage_worker_later_match_wins_despite_earlier_adjudication_failure(
    make_event: Any, tenant_id: Any
) -> None:
    """One candidate's LLM check times out, but a later candidate is a genuine
    deterministic match -- the worker should still link to it rather than raising.
    """
    # Arrange
    matched_ctx_id = uuid4()
    event = make_event(
        content_clean="Implementing router for JIRA-123",
        metadata={
            "clustering_matches": [
                {
                    "context_id": str(uuid4()),
                    "summary": "No identifiers here, needs LLM check",
                    "confidence_score": 0.5,
                    "similarity": 0.90,
                },
                {
                    "context_id": str(matched_ctx_id),
                    "summary": "This is related to JIRA-123 decision",
                    "confidence_score": 0.5,
                    "similarity": 0.92,
                },
            ]
        },
    )
    mock_bus = AsyncMock()
    mock_engine = MagicMock()

    async def slow_validate(*args, **kwargs):
        await asyncio.sleep(0.5)
        return "same"

    mock_llm = AsyncMock(spec=LLMClient)
    mock_llm.judge_linkage.side_effect = slow_validate

    mock_store = AsyncMock(spec=PostgresEventStore)
    mock_store.get_event_hold.return_value = (True, None)
    mock_store.get_record_holds.side_effect = lambda _tenant, ids: dict.fromkeys(ids)

    with (
        patch("checkcord.pipeline.linkage.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.linkage.PostgresEventStore", return_value=mock_store),
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = MagicMock()
        mock_tenant_context.return_value = mock_ctx

        worker = LinkageWorker(
            bus=mock_bus,
            engine=mock_engine,
            llm_client=mock_llm,
            llm_timeout_seconds=0.05,
        )

        # Act
        await worker.process_message(event)

        # Assert: linked to the deterministic match found after the timeout,
        # no exception raised, no duplicate bootstrapped
        mock_store.link_context_to_event.assert_called_once_with(
            tenant_id=tenant_id,
            canonical_id=matched_ctx_id,
            event_id=event.event_id,
            excerpt=_STORED,
        )
        mock_store.save_canonical_context.assert_not_called()


async def test_linkage_worker_deterministic_mismatch(make_event: Any, tenant_id: Any) -> None:
    # Arrange
    event = make_event(
        content_clean="Fix issue JIRA-456",
        metadata={
            "embedding": [0.3] * 1536,
            "clustering_matches": [
                {
                    "context_id": str(uuid4()),
                    "summary": "Existing context discussing JIRA-123",
                    "confidence_score": 0.5,
                    "similarity": 0.90,
                }
            ],
        },
    )
    mock_bus = AsyncMock()
    mock_engine = MagicMock()
    mock_llm = AsyncMock(spec=LLMClient)

    mock_store = AsyncMock(spec=PostgresEventStore)
    mock_store.get_event_hold.return_value = (True, None)
    mock_store.get_record_holds.side_effect = lambda _tenant, ids: dict.fromkeys(ids)
    new_ctx_id = uuid4()
    mock_store.save_canonical_context.return_value = new_ctx_id

    with (
        patch("checkcord.pipeline.linkage.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.linkage.PostgresEventStore", return_value=mock_store),
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = MagicMock()
        mock_tenant_context.return_value = mock_ctx

        worker = LinkageWorker(bus=mock_bus, engine=mock_engine, llm_client=mock_llm)

        # Act
        await worker.process_message(event)

        # Assert
        # LLM must NOT be called because both sides have identifiers that do not match
        mock_llm.judge_linkage.assert_not_called()

        # Must bootstrap a new context
        mock_store.save_canonical_context.assert_called_once_with(
            tenant_id=tenant_id,
            summary=event.content_clean,
            confidence_score=0.0,
            embedding=[0.3] * 1536,
            access_control=event.access_control.model_dump(mode="python"),
            scope_repo_identifier=None,
            context_type=None,
            base_event_id=event.event_id,
            held_connection=None,
            redaction=_STORED,
        )
        mock_store.link_context_to_event.assert_called_once_with(
            tenant_id=tenant_id,
            canonical_id=new_ctx_id,
            event_id=event.event_id,
        )


async def test_restatement_rule() -> None:
    assert is_restatement("Money is integer cents.", "money is  INTEGER cents", 0.5)
    assert is_restatement("Something else entirely", "Money is integer cents", 0.97)
    assert not is_restatement("Money is integer cents, refunds too", "Money is integer cents", 0.9)


async def test_cut_excerpt() -> None:
    assert cut_excerpt("  short  ") == "short"
    sentences = ("This sentence is about twenty-nine. " * 30).strip()
    cut = cut_excerpt(sentences)
    assert len(cut) <= 601 and cut.endswith(".…")
    words = "word " * 200
    cut = cut_excerpt(words)
    assert cut.endswith("word…") and len(cut) <= 601
    assert cut_excerpt("x" * 700) == "x" * 600 + "…"


async def test_update_excerpt_skips_a_restatement() -> None:
    match = {"summary": "Money is integer cents.", "similarity": 0.9}
    assert update_excerpt("money is integer cents", match) is None
    assert update_excerpt("Money is integer cents; refunds too.", match) == (
        "Money is integer cents; refunds too."
    )


def _changes_setup(
    make_event: Any, *, new_type: str | None, old_type: str | None, scope: list[Any]
) -> tuple[Any, Any, Any, Any]:
    old_id = uuid4()
    event = make_event(
        content_clean="Path versions are going away; versioning moves to a header.",
        metadata={
            "context_type": new_type,
            "clustering_matches": [
                {
                    "context_id": str(old_id),
                    "summary": "API versions go in the path.",
                    "similarity": 0.8,
                }
            ],
        },
    )
    mock_llm = AsyncMock(spec=LLMClient)
    mock_llm.judge_linkage.return_value = "changes"
    store = AsyncMock(spec=PostgresEventStore)
    store.get_event_hold.return_value = (True, None)
    store.get_record_holds.side_effect = lambda _tenant, ids: dict.fromkeys(ids)
    store.get_canonical_context.return_value = {
        "context_type": old_type,
        "retired_at": None,
        "held_connection": None,
    }
    store.get_context_scope_rows.return_value = scope
    store.save_canonical_context.return_value = uuid4()
    return event, old_id, mock_llm, store


@pytest.mark.parametrize(
    ("new_type", "old_type", "scope", "suggested"),
    [
        ("adr", "constraint", [], True),
        ("adr", "risk", [], False),
        ("adr", "adr", [{"scope_type": "repo", "scope_value": "acme/api"}], False),
    ],
)
async def test_changes_verdict_starts_a_record(
    make_event: Any, new_type: str, old_type: str, scope: list[Any], suggested: bool
) -> None:
    event, old_id, mock_llm, store = _changes_setup(
        make_event, new_type=new_type, old_type=old_type, scope=scope
    )
    with (
        patch("checkcord.pipeline.linkage.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.linkage.PostgresEventStore", return_value=store),
        patch("checkcord.pipeline.linkage.records.suggest", AsyncMock()) as suggest,
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = MagicMock()
        mock_tenant_context.return_value = mock_ctx
        worker = LinkageWorker(bus=AsyncMock(), engine=MagicMock(), llm_client=mock_llm)
        await worker.process_message(event)

    store.save_canonical_context.assert_called_once()
    assert store.link_context_to_event.call_args.kwargs["canonical_id"] != old_id
    if suggested:
        suggest.assert_awaited_once()
        assert suggest.call_args.args[2] == old_id and suggest.call_args.args[4] == "model"
    else:
        suggest.assert_not_called()


async def test_a_changes_verdict_after_a_failed_call_does_not_raise(make_event: Any) -> None:
    event, _, mock_llm, store = _changes_setup(make_event, new_type="adr", old_type="adr", scope=[])
    event.metadata["clustering_matches"].insert(
        0, {"context_id": str(uuid4()), "summary": "Unrelated.", "similarity": 0.9}
    )
    mock_llm.judge_linkage.side_effect = [RuntimeError("down"), "changes"]
    with (
        patch("checkcord.pipeline.linkage.tenant_context") as mock_tenant_context,
        patch("checkcord.pipeline.linkage.PostgresEventStore", return_value=store),
        patch("checkcord.pipeline.linkage.records.suggest", AsyncMock()) as suggest,
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = MagicMock()
        mock_tenant_context.return_value = mock_ctx
        worker = LinkageWorker(bus=AsyncMock(), engine=MagicMock(), llm_client=mock_llm)
        await worker.process_message(event)
    store.save_canonical_context.assert_called_once()
    suggest.assert_awaited_once()

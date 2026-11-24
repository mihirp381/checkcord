import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from checkcord.models.common import AccessControl
from checkcord.models.enums import SourcePlatform
from checkcord.services.context_formatter import ContextFormatterService
from checkcord.services.repo_audience import RepoAudience
from checkcord.storage.postgres import PostgresEventStore

pytestmark = pytest.mark.asyncio

PUBLIC = {"is_public": True}


async def test_context_formatter_compile_success() -> None:
    # Arrange
    tenant_id = uuid4()
    repo_identifier = "my-org/my-repo"

    mock_store = AsyncMock(spec=PostgresEventStore)

    # 1. Mock get_active_contexts_for_repo_compilation
    ctx_1_id = uuid4()
    ctx_2_id = uuid4()
    mock_store.get_active_contexts_for_repo_compilation.return_value = [
        {
            "id": ctx_1_id,
            "summary": "Do not modify Auth.login directly.",
            "confidence_score": 0.95,
            "created_at": datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC),
            "access_control": PUBLIC,
        },
        {
            "id": ctx_2_id,
            "summary": "Use PostgresEventStore for event persistence.",
            "confidence_score": 0.88,
            "created_at": datetime(2026, 7, 5, 12, 10, 0, tzinfo=UTC),
            "access_control": PUBLIC,
        },
    ]

    # 2. Mock get_symbols_for_context_in_repo
    mock_store.get_symbols_for_context_in_repo.side_effect = lambda t, cid, r: (
        [{"symbol_name": "Auth.login", "file_path": "auth.py"}]
        if cid == ctx_1_id
        else [{"symbol_name": "PostgresEventStore", "file_path": "postgres.py"}]
    )

    # 3. Mock get_events_for_context and get_events_by_ids
    mock_store.get_events_for_context.return_value = [uuid4()]

    mock_event = MagicMock()
    mock_event.source_platform = SourcePlatform.SLACK
    mock_event.source_id = "1234567890.123456"
    mock_event.metadata = {"channel_id": "C12345"}
    mock_event.access_control = AccessControl(is_public=True)
    mock_store.get_events_by_ids.return_value = [mock_event]

    # 4. Mock get_repo_ledger_mappings
    mock_store.get_repo_ledger_mappings.return_value = [
        {"symbol_name": "Auth.login", "canonical_id": ctx_1_id},
        {"symbol_name": "PostgresEventStore", "canonical_id": ctx_2_id},
    ]

    formatter = ContextFormatterService()

    # Act
    architecture_md, ledger_json, _ = await formatter.compile_for_repo(
        tenant_id, repo_identifier, mock_store, RepoAudience()
    )

    # Assert
    assert f"tenant_id: {tenant_id}" in architecture_md
    assert f"repo_identifier: {repo_identifier}" in architecture_md
    assert "### Do not modify Auth.login directly." in architecture_md
    assert "### Use PostgresEventStore for event persistence." in architecture_md
    assert "https://slack.com/archives/C12345/p1234567890123456" in architecture_md
    assert "# Architecture decisions\n" in architecture_md
    assert "#### Code\n- `Auth.login` (File: `auth.py`)" in architecture_md

    ledger = json.loads(ledger_json)
    assert ledger == {
        "Auth.login": [str(ctx_1_id)],
        "PostgresEventStore": [str(ctx_2_id)],
    }


async def test_context_formatter_groups_typed_contexts_into_sections() -> None:
    """Mixed context_type contexts render under typed section headings."""
    tenant_id = uuid4()
    repo_identifier = "my-org/my-repo"

    mock_store = AsyncMock(spec=PostgresEventStore)

    adr_id = uuid4()
    constraint_id = uuid4()
    risk_id = uuid4()
    mock_store.get_active_contexts_for_repo_compilation.return_value = [
        {
            "id": adr_id,
            "summary": "Decision: use Postgres.",
            "confidence_score": 0.9,
            "created_at": datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC),
            "access_control": PUBLIC,
            "context_type": "adr",
        },
        {
            "id": constraint_id,
            "summary": "Must run on Postgres.",
            "confidence_score": 0.85,
            "created_at": datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC),
            "access_control": PUBLIC,
            "context_type": "constraint",
        },
        {
            "id": risk_id,
            "summary": "Downtime risk during migration.",
            "confidence_score": 0.5,
            "created_at": datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC),
            "access_control": PUBLIC,
            "context_type": "risk",
        },
    ]
    mock_store.get_symbols_for_context_in_repo.return_value = []
    mock_store.get_events_for_context.return_value = []
    mock_store.get_repo_ledger_mappings.return_value = []

    formatter = ContextFormatterService()
    architecture_md, _, _ = await formatter.compile_for_repo(
        tenant_id, repo_identifier, mock_store, RepoAudience()
    )

    assert "### Decisions" in architecture_md
    assert "### Constraints" in architecture_md
    assert "### Risks" in architecture_md
    assert "Decision: use Postgres." in architecture_md
    assert "Must run on Postgres." in architecture_md
    assert "Downtime risk during migration." in architecture_md
    # Decisions section must come before Constraints, which comes before Risks.
    assert architecture_md.index("### Decisions") < architecture_md.index("### Constraints")
    assert architecture_md.index("### Constraints") < architecture_md.index("### Risks")


async def test_context_formatter_falls_back_to_flat_rendering_when_all_general() -> None:
    """A repo with no typed contexts renders without type section headings."""
    tenant_id = uuid4()
    repo_identifier = "my-org/my-repo"

    mock_store = AsyncMock(spec=PostgresEventStore)
    ctx_id = uuid4()
    mock_store.get_active_contexts_for_repo_compilation.return_value = [
        {
            "id": ctx_id,
            "summary": "A general architectural rule.",
            "confidence_score": 0.7,
            "created_at": datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC),
            "access_control": PUBLIC,
            "context_type": "general",
        },
    ]
    mock_store.get_symbols_for_context_in_repo.return_value = []
    mock_store.get_events_for_context.return_value = []
    mock_store.get_repo_ledger_mappings.return_value = []

    formatter = ContextFormatterService()
    architecture_md, _, _ = await formatter.compile_for_repo(
        tenant_id, repo_identifier, mock_store, RepoAudience()
    )

    assert "### General Rules" not in architecture_md
    assert "### Decisions" not in architecture_md
    assert "### A general architectural rule." in architecture_md


async def test_context_formatter_defaults_missing_context_type_to_general() -> None:
    """A store row without context_type must not KeyError; it defaults to
    'general', the DB column default.
    """
    tenant_id = uuid4()
    repo_identifier = "my-org/my-repo"

    mock_store = AsyncMock(spec=PostgresEventStore)
    mock_store.get_active_contexts_for_repo_compilation.return_value = [
        {
            "id": uuid4(),
            "summary": "No context_type key at all.",
            "confidence_score": 0.6,
            "created_at": datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC),
            "access_control": PUBLIC,
        },
    ]
    mock_store.get_symbols_for_context_in_repo.return_value = []
    mock_store.get_events_for_context.return_value = []
    mock_store.get_repo_ledger_mappings.return_value = []

    formatter = ContextFormatterService()
    architecture_md, _, _ = await formatter.compile_for_repo(
        tenant_id, repo_identifier, mock_store, RepoAudience()
    )

    assert "### No context_type key at all." in architecture_md
    assert "### Decisions" not in architecture_md


async def test_context_formatter_leaves_out_records_the_audience_cannot_see() -> None:
    tenant_id = uuid4()
    alice, bob = str(uuid4()), str(uuid4())
    shared_id, alice_only_id, empty_id = uuid4(), uuid4(), uuid4()

    def ctx(cid: object, summary: str, access: dict[str, object]) -> dict[str, object]:
        return {
            "id": cid,
            "summary": summary,
            "confidence_score": 0.9,
            "created_at": datetime(2026, 8, 4, tzinfo=UTC),
            "access_control": access,
        }

    mock_store = AsyncMock(spec=PostgresEventStore)
    mock_store.get_active_contexts_for_repo_compilation.return_value = [
        ctx(shared_id, "SHARED-RULE", {"is_public": False, "allowed_identity_ids": [alice, bob]}),
        ctx(
            alice_only_id, "ALICE-ONLY-RULE", {"is_public": False, "allowed_identity_ids": [alice]}
        ),
        ctx(empty_id, "EMPTY-AUDIENCE-RULE", {"is_public": False, "allowed_identity_ids": []}),
    ]
    mock_store.get_symbols_for_context_in_repo.return_value = []
    mock_store.get_events_for_context.return_value = []
    mock_store.get_repo_ledger_mappings.return_value = [
        {"symbol_name": "shared_fn", "canonical_id": shared_id},
        {"symbol_name": "secret_fn", "canonical_id": alice_only_id},
        {"symbol_name": "shared_fn", "canonical_id": empty_id},
    ]

    compiled = await ContextFormatterService().compile_for_repo(
        tenant_id, "my-org/my-repo", mock_store, RepoAudience(frozenset({alice, bob}))
    )

    assert "SHARED-RULE" in compiled.architecture_md
    assert "ALICE-ONLY-RULE" not in compiled.architecture_md
    assert "EMPTY-AUDIENCE-RULE" not in compiled.architecture_md
    assert str(alice_only_id) not in compiled.architecture_md
    assert json.loads(compiled.ledger_json) == {"shared_fn": [str(shared_id)]}
    assert compiled.context_ids == [str(shared_id)]


async def test_context_formatter_unknown_audience_keeps_only_public_records() -> None:
    mock_store = AsyncMock(spec=PostgresEventStore)
    member = str(uuid4())
    mock_store.get_active_contexts_for_repo_compilation.return_value = [
        {
            "id": uuid4(),
            "summary": "PUBLIC-RULE",
            "confidence_score": 0.5,
            "access_control": PUBLIC,
        },
        {
            "id": uuid4(),
            "summary": "MEMBER-RULE",
            "confidence_score": 0.5,
            "access_control": {"is_public": False, "allowed_identity_ids": [member]},
        },
    ]
    mock_store.get_symbols_for_context_in_repo.return_value = []
    mock_store.get_events_for_context.return_value = []
    mock_store.get_repo_ledger_mappings.return_value = []

    compiled = await ContextFormatterService().compile_for_repo(
        uuid4(), "my-org/my-repo", mock_store, RepoAudience()
    )

    assert "PUBLIC-RULE" in compiled.architecture_md
    assert "MEMBER-RULE" not in compiled.architecture_md


async def test_context_formatter_redacts_summaries() -> None:
    mock_store = AsyncMock(spec=PostgresEventStore)
    mock_store.get_active_contexts_for_repo_compilation.return_value = [
        {
            "id": uuid4(),
            "summary": "Ask carol@example.com; key ghp_abcdefghijklmnopqrstuvwxyz0123",
            "confidence_score": 0.5,
            "access_control": PUBLIC,
        }
    ]
    mock_store.get_symbols_for_context_in_repo.return_value = []
    mock_store.get_events_for_context.return_value = []
    mock_store.get_repo_ledger_mappings.return_value = []

    compiled = await ContextFormatterService().compile_for_repo(
        uuid4(), "my-org/my-repo", mock_store, RepoAudience()
    )

    assert "carol@example.com" not in compiled.architecture_md
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123" not in compiled.architecture_md
    assert "[REDACTED:EMAIL]" in compiled.architecture_md
    assert "[REDACTED:TOKEN]" in compiled.architecture_md


async def test_context_file_shows_updates_and_a_pending_replacement() -> None:
    from unittest.mock import patch

    tenant_id = uuid4()
    record_id = uuid4()
    mock_store = AsyncMock(spec=PostgresEventStore)
    mock_store.get_active_contexts_for_repo_compilation.return_value = [
        {
            "id": record_id,
            "summary": "Money is integer cents.",
            "confidence_score": 3.0,
            "created_at": datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC),
            "access_control": PUBLIC,
        }
    ]
    mock_store.get_symbols_for_context_in_repo.return_value = []
    mock_store.get_events_for_context.return_value = []
    mock_store.get_repo_ledger_mappings.return_value = []

    async def annotate(conn, tenant, rows, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["allows"] is not None
        rows[0]["updates"] = [
            {"happened_at": "2026-09-02T00:00:00+00:00", "source": {}, "text": "Refunds too."},
            {"happened_at": "2026-09-01T00:00:00+00:00", "source": {}, "text": "Shipped."},
        ]
        rows[0]["may_be_replaced_by"] = {
            "record_id": "r2",
            "created_at": "2026-09-03T00:00:00+00:00",
            "basis": "stated",
            "summary": "Money is decimal.",
        }
        return rows

    with patch("checkcord.services.context_formatter.record_notes.annotate", annotate):
        md, _, _ = await ContextFormatterService().compile_for_repo(
            tenant_id, "acme/api", mock_store, RepoAudience(), conn=AsyncMock()
        )
    assert "#### Updates\n- 2026-09-02: Refunds too.\n- 2026-09-01: Shipped." in md
    assert "- **May be replaced by:** `r2` (stated)\n" in md
    assert md.count("May be replaced by") == 1
    assert "#### Code" not in md
    assert "**Source:**" not in md
    assert "  may_be_replaced_by: r2\n" in md


async def test_front_matter_round_trips_awkward_summaries() -> None:
    import yaml

    summary = 'Line one: "quoted"\n---\nkey: value # not a comment'
    store = AsyncMock(spec=PostgresEventStore)
    store.get_active_contexts_for_repo_compilation.return_value = [
        {
            "id": uuid4(),
            "summary": summary,
            "confidence_score": 0.9,
            "context_type": "constraint",
            "created_at": datetime(2026, 7, 5, tzinfo=UTC),
            "access_control": PUBLIC,
        }
    ]
    store.get_symbols_for_context_in_repo.return_value = []
    store.get_events_for_context.return_value = []
    store.get_repo_ledger_mappings.return_value = []

    md, _, _ = await ContextFormatterService().compile_for_repo(
        uuid4(), "o/r", store, RepoAudience()
    )

    assert md.startswith("---\n")
    end = md.index("\n---\n", 4)
    parsed = yaml.safe_load(md[4:end])
    assert parsed["contexts"][0]["summary"] == summary
    assert parsed["repo_identifier"] == "o/r"
    assert '### Line one: "quoted"\n' in md
    assert "Confidence" not in md
    assert "Rule:" not in md


async def test_heading_is_first_line_cut_with_full_text_when_longer() -> None:
    from checkcord.services.context_formatter import heading

    assert heading("Short decision.") == ("Short decision.", None)
    long = "x" * 130
    assert heading(long) == ("x" * 120, long)
    assert heading("Title\nmore") == ("Title", "more")


async def test_a_two_line_summary_prints_its_first_line_once() -> None:
    store = AsyncMock(spec=PostgresEventStore)
    store.get_active_contexts_for_repo_compilation.return_value = [
        {
            "id": uuid4(),
            "summary": "Retries stop after 3 attempts.\n- Card networks fine repeated retries",
            "confidence_score": 1.0,
            "context_type": "adr",
            "created_at": datetime(2026, 7, 5, tzinfo=UTC),
            "access_control": PUBLIC,
        }
    ]
    store.get_symbols_for_context_in_repo.return_value = []
    store.get_events_for_context.return_value = []
    store.get_repo_ledger_mappings.return_value = []
    md, _, _ = await ContextFormatterService().compile_for_repo(
        uuid4(), "o/r", store, RepoAudience()
    )
    body = md[md.index("## Active Decisions") :]
    assert body.count("Retries stop after 3 attempts.") == 1
    assert "### Retries stop after 3 attempts.\n\n- Card networks fine repeated retries\n" in body
    assert "may_be_replaced_by" not in md

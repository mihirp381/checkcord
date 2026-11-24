# ruff: noqa: ASYNC101
"""Meeting transcripts through a real model and Postgres."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.core.config import get_settings
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.storage.postgres import tenant_context
from checkcord.workers.transcript_worker import TranscriptExtractionWorker

# Ensure environment variables are loaded so OPENAI_API_KEY is available
load_dotenv()

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.live("OPENAI_API_KEY")]

TRANSCRIPTS = Path(__file__).parent.parent / "fixtures" / "meeting_transcripts"
TRANSCRIPT_FILES = sorted(str(p) for p in TRANSCRIPTS.glob("*.txt"))

if not TRANSCRIPT_FILES:
    pytest.skip("No meeting transcripts found.", allow_module_level=True)


def make_meeting_event(content: str, tenant_id: UUID) -> CanonicalEvent:
    return CanonicalEvent(
        tenant_id=tenant_id,
        source_platform=SourcePlatform.ZOOM,
        source_type=SourceType.MEETING_TRANSCRIPT,
        source_id=str(uuid4()),
        happened_at=datetime.now(UTC),
        author=AuthorInfo(source_id="host_test"),
        content_raw=content,
        content_clean=content,
        access_control=AccessControl(is_public=True),
    )


@pytest.mark.parametrize("filepath", TRANSCRIPT_FILES)
async def test_real_llm_pipeline_e2e(filepath: str) -> None:
    """Run GPT-4o on a meeting transcript and check what is stored."""
    # Initialize LLM
    real_llm_client = ChatOpenAI(model_name="gpt-4o", temperature=0.1, max_retries=5)

    # Initialize Engine
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    # Ensure RLS user exists for strict testing (matches test_meeting_db.py)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "DO $$\n"
                "BEGIN\n"
                "    IF NOT EXISTS (\n"
                "        SELECT FROM pg_catalog.pg_roles WHERE rolname = 'rls_test_user'\n"
                "    ) THEN\n"
                "        CREATE ROLE rls_test_user WITH LOGIN;\n"
                "    END IF;\n"
                "END\n"
                "$$;"
            )
        )
        await conn.execute(text("GRANT USAGE ON SCHEMA public TO rls_test_user"))
        await conn.execute(
            text("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO rls_test_user")
        )

    # We don't want to actually connect to Redis for the test, just mock the bus
    mock_bus = MagicMock()

    # Initialize worker with real LLM and Real DB Engine
    worker = TranscriptExtractionWorker(
        bus=mock_bus,
        engine=engine,
        llm_client=real_llm_client,
        max_concurrency=10,
        sync_judge=True,
    )

    with open(filepath) as f:  # noqa: ASYNC230
        transcript_content = f.read()

    tenant_id = uuid4()
    event = make_meeting_event(transcript_content, tenant_id)

    # Run the worker message processor
    await worker.process_message(event)

    # Assertions on in-memory collections populated by worker
    assert len(worker.processed_segments) > 0, f"No segments produced for {filepath}"

    total_extractions = (
        len(worker.extracted_adrs) + len(worker.extracted_constraints) + len(worker.extracted_risks)
    )

    assert total_extractions > 0, f"Zero valid extractions produced for {filepath}"

    # Assertions on Database Persistence
    async with tenant_context(engine, tenant_id) as conn:
        await conn.execute(text("SET LOCAL ROLE rls_test_user"))

        # Verify segments were saved
        res_seg = await conn.execute(
            text("SELECT count(*) FROM meeting_discourse_segments WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
        assert res_seg.scalar() > 0

        # Verify extractions were saved
        res_adr = await conn.execute(
            text("SELECT count(*) FROM extracted_adrs WHERE tenant_id = :tid"), {"tid": tenant_id}
        )
        res_con = await conn.execute(
            text("SELECT count(*) FROM extracted_constraints WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
        res_risk = await conn.execute(
            text("SELECT count(*) FROM extracted_risks WHERE tenant_id = :tid"), {"tid": tenant_id}
        )

        db_total_extractions = res_adr.scalar() + res_con.scalar() + res_risk.scalar()

        assert db_total_extractions == total_extractions, (
            f"DB mismatch. Expected {total_extractions}, got {db_total_extractions} in DB."
        )

    await engine.dispose()

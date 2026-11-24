from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.adapters.meetings.parser import (
    VttCue,
    chunk_cues,
    parse_and_chunk_transcript,
    parse_vtt,
    resolve_meeting_speaker,
)
from checkcord.core.config import get_settings
from checkcord.models.enums import SourcePlatform

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_parse_vtt_xml_style() -> None:
    vtt = """WEBVTT

1
00:00:01.000 --> 00:00:04.500
<v Alice Admin>Hello this is Alice speaking.

2
00:00:05.123 --> 00:00:08.567
<v Bob Builder>Hi Alice, I am Bob.
"""
    cues = parse_vtt(vtt)
    assert len(cues) == 2
    assert cues[0].speaker == "Alice Admin"
    assert cues[0].text == "Hello this is Alice speaking."
    assert cues[0].start_offset == 1.0
    assert cues[0].end_offset == 4.5
    assert cues[1].speaker == "Bob Builder"
    assert cues[1].text == "Hi Alice, I am Bob."
    assert cues[1].start_offset == 5.123
    assert cues[1].end_offset == 8.567


async def test_parse_vtt_prefix_style() -> None:
    vtt = """WEBVTT

00:01.000 --> 00:03.500
Charlie: Hey guys!

00:04.000 --> 00:06.000
Dave:
What's up?
"""
    cues = parse_vtt(vtt)
    assert len(cues) == 2
    assert cues[0].speaker == "Charlie"
    assert cues[0].text == "Hey guys!"
    assert cues[0].start_offset == 1.0
    assert cues[0].end_offset == 3.5
    assert cues[1].speaker == "Dave"
    assert cues[1].text == "What's up?"
    assert cues[1].start_offset == 4.0
    assert cues[1].end_offset == 6.0


async def test_chunk_cues_character_limit() -> None:
    cues = [
        VttCue(0, 5, "Alice", "Hello " * 100),  # 600 chars formatted
        VttCue(5, 10, "Bob", "Hi " * 100),  # 300 chars formatted
        VttCue(10, 15, "Charlie", "Yo " * 1000),  # 3000 chars formatted
    ]
    chunks = chunk_cues(cues, max_chars=1000)
    # The first two fit together (600 + 300 = 900 < 1000)
    # The third is 3000 chars and starts a new chunk.
    assert len(chunks) == 2
    assert len(chunks[0]) == 2
    assert len(chunks[1]) == 1


async def test_resolve_meeting_speaker_lifecycle() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    tenant_id = uuid4()
    alice_uid = uuid4()
    bob_ent_uid = uuid4()
    bob_chat_uid = uuid4()

    # Seed test users
    async with engine.begin() as conn:
        # Bypass RLS to insert seed data
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))

        # Enterprise Users
        await conn.execute(
            text(
                "INSERT INTO enterprise_users (id, tenant_id, primary_email, display_name, active) "
                "VALUES (:id, :tenant_id, 'alice@example.com', 'Alice Admin', True)"
            ),
            {"id": alice_uid, "tenant_id": tenant_id},
        )
        await conn.execute(
            text(
                "INSERT INTO enterprise_users (id, tenant_id, primary_email, display_name, active) "
                "VALUES (:id, :tenant_id, 'bob@example.com', 'Bob Internal', True)"
            ),
            {"id": bob_ent_uid, "tenant_id": tenant_id},
        )
        # Chat User
        await conn.execute(
            text(
                "INSERT INTO chat_users "
                "(id, tenant_id, platform, native_id, display_name, email, internal_id) "
                "VALUES (:id, :tenant_id, 'slack', 'U_BOB', "
                "'Bob Builder', 'bob@example.com', :internal_id)"
            ),
            {"id": bob_chat_uid, "tenant_id": tenant_id, "internal_id": bob_ent_uid},
        )

    try:
        # 1. Resolve by exact email
        uid, email, dname = await resolve_meeting_speaker(engine, tenant_id, "alice@example.com")
        assert uid == alice_uid
        assert email == "alice@example.com"
        assert dname == "Alice Admin"

        # 2. Resolve by exact display name
        uid, email, dname = await resolve_meeting_speaker(engine, tenant_id, "Alice Admin")
        assert uid == alice_uid
        assert email == "alice@example.com"
        assert dname == "Alice Admin"

        # 3. Resolve by chat user display name
        uid, email, dname = await resolve_meeting_speaker(engine, tenant_id, "Bob Builder")
        assert uid == bob_ent_uid
        assert email == "bob@example.com"
        assert dname == "Bob Builder"

        # 4. Resolve unresolvable
        uid, email, dname = await resolve_meeting_speaker(engine, tenant_id, "Dave Unknown")
        assert uid is None
        assert email is None
        assert dname == "Dave Unknown"

    finally:
        # Cleanup
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM chat_users WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
            await conn.execute(
                text("DELETE FROM enterprise_users WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )


async def test_parse_and_chunk_transcript_lifecycle() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    tenant_id = uuid4()
    alice_uid = uuid4()

    # Seed Alice as host
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text(
                "INSERT INTO enterprise_users (id, tenant_id, primary_email, display_name, active) "
                "VALUES (:id, :tenant_id, 'alice@example.com', 'Alice Admin', True)"
            ),
            {"id": alice_uid, "tenant_id": tenant_id},
        )

    vtt = """WEBVTT

1
00:00:01.000 --> 00:00:03.000
<v alice@example.com>Hello everyone.

2
00:00:04.000 --> 00:00:06.000
<v Bob Builder>Hello.
"""

    try:
        events = await parse_and_chunk_transcript(
            vtt_text=vtt,
            db_engine=engine,
            tenant_id=tenant_id,
            meeting_id="meet-xyz",
            meeting_topic="Weekly Sync",
            meeting_start=datetime.now(UTC),
            host_email_or_name="alice@example.com",
            platform=SourcePlatform.ZOOM,
        )

        assert len(events) == 1
        ev = events[0]
        assert ev.tenant_id == tenant_id
        assert ev.source_platform == SourcePlatform.ZOOM
        assert ev.author.internal_id == alice_uid
        assert ev.author.email == "alice@example.com"
        assert ev.access_control.is_public is False
        assert alice_uid in ev.access_control.allowed_identity_ids

        # Bob is unresolved but his name is in the transcript content
        assert "Bob Builder: Hello." in ev.content_clean
        assert "[Chunk 1 of 1]" in ev.content_raw

        # Assert meeting_metadata
        assert ev.meeting_metadata is not None
        assert ev.meeting_metadata.duration_seconds == 6
        assert ev.meeting_metadata.attendee_count == 2

        # Test active meeting rejection (duration <= 0)
        ongoing_events = await parse_and_chunk_transcript(
            vtt_text=vtt,
            db_engine=engine,
            tenant_id=tenant_id,
            meeting_id="meet-active",
            meeting_topic="Active Meeting",
            meeting_start=datetime.now(UTC),
            host_email_or_name="alice@example.com",
            platform=SourcePlatform.ZOOM,
            duration_seconds=0,  # 0 or negative = active meeting
        )
        assert ongoing_events == []  # Rejected

    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
            await conn.execute(
                text("DELETE FROM enterprise_users WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )

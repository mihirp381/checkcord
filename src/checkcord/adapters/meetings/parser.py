"""WebVTT transcripts: cues, speakers matched to people, and chunks."""

import re
from datetime import datetime
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.models.common import AccessControl, AuthorInfo, MeetingMetadata
from checkcord.models.enums import (
    AccessVisibility,
    AclType,
    EventAction,
    SignalStatus,
    SourcePlatform,
    SourceType,
)
from checkcord.models.event import CanonicalEvent
from checkcord.storage.models import ChatUser, EnterpriseUser
from checkcord.storage.postgres import tenant_context


class VttCue(NamedTuple):
    start_offset: float  # seconds from start
    end_offset: float  # seconds from start
    speaker: str
    text: str


def parse_timestamp_seconds(ts_str: str) -> float:
    """Parse WEBWTT timestamp string into float seconds."""
    ts_str = ts_str.replace(",", ".")
    parts = ts_str.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return float(h) * 3600 + float(m) * 60 + float(s)
    elif len(parts) == 2:
        m, s = parts
        return float(m) * 60 + float(s)
    else:
        return float(parts[0])


def parse_vtt(vtt_text: str) -> list[VttCue]:
    """Parse WebVTT file content into a list of VttCue NamedTuples."""
    blocks = vtt_text.strip().split("\n\n")
    cues = []

    # Regex to match timestamp lines like: "00:00:01.000 --> 00:00:04.500"
    ts_pattern = re.compile(r"(\d{2}:)?\d{2}:\d{2}[.,]\d{3}\s+-->\s+(\d{2}:)?\d{2}:\d{2}[.,]\d{3}")

    for block in blocks:
        lines = [line.strip() for line in block.strip().split("\n") if line.strip()]
        if not lines:
            continue

        # Find the line containing the timestamp
        ts_line_idx = -1
        for i, line in enumerate(lines):
            if ts_pattern.search(line):
                ts_line_idx = i
                break

        if ts_line_idx == -1:
            continue

        ts_line = lines[ts_line_idx]
        text_lines = lines[ts_line_idx + 1 :]

        # Parse timestamp
        parts = ts_line.split("-->")
        if len(parts) != 2:
            continue

        try:
            start_sec = parse_timestamp_seconds(parts[0].strip())
            end_sec = parse_timestamp_seconds(parts[1].strip())
        except ValueError:
            continue

        # Reconstruct the raw text
        raw_text = " ".join(text_lines).strip()
        if not raw_text:
            continue

        speaker = "Unknown Speaker"

        # Parse `<v Speaker Name>Text` tag format
        v_match = re.search(r"<v\s+([^>]+)>(.*)", raw_text, re.DOTALL)
        if v_match:
            speaker = v_match.group(1).strip()
            raw_text = v_match.group(2).strip()
            raw_text = re.sub(r"</v>", "", raw_text).strip()
        else:
            # Parse `Speaker Name: Text` prefix format
            colon_parts = raw_text.split(":", 1)
            if (
                len(colon_parts) == 2
                and len(colon_parts[0]) < 50
                and not colon_parts[0].startswith("http")
            ):
                candidate = colon_parts[0].strip()
                if candidate and "\n" not in candidate:
                    speaker = candidate
                    raw_text = colon_parts[1].strip()

        # Clean any remaining HTML/XML tags
        raw_text = re.sub(r"<[^>]+>", "", raw_text).strip()

        if not raw_text:
            continue

        cues.append(
            VttCue(
                start_offset=start_sec,
                end_offset=end_sec,
                speaker=speaker,
                text=raw_text,
            )
        )

    return cues


def format_cue_line(cue: VttCue) -> str:
    """Format a single cue into standard speaker line."""
    h = int(cue.start_offset // 3600)
    m = int((cue.start_offset % 3600) // 60)
    s = int(cue.start_offset % 60)
    return f"[{h:02d}:{m:02d}:{s:02d}] {cue.speaker}: {cue.text}"


def chunk_cues(cues: list[VttCue], max_chars: int = 4000) -> list[list[VttCue]]:
    """Group cues into chunks respecting speaker boundaries and max character length."""
    chunks: list[list[VttCue]] = []
    current_chunk: list[VttCue] = []
    current_len = 0

    for cue in cues:
        line = format_cue_line(cue) + "\n"
        line_len = len(line)

        if current_chunk and current_len + line_len > max_chars:
            chunks.append(current_chunk)
            current_chunk = [cue]
            current_len = line_len
        else:
            current_chunk.append(cue)
            current_len += line_len

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


async def resolve_meeting_speaker(
    db_engine: AsyncEngine,
    tenant_id: UUID,
    email_or_name: str,
) -> tuple[UUID | None, str | None, str]:
    """Resolve speaker name or email to an EnterpriseUser.

    Returns (internal_id, email, display_name).
    """
    import structlog

    logger = structlog.get_logger(__name__)
    logger.info(
        "resolve_meeting_speaker.start", tenant_id=str(tenant_id), email_or_name=email_or_name
    )

    async with tenant_context(db_engine, tenant_id) as conn:
        # Email lookup
        if "@" in email_or_name:
            stmt = select(EnterpriseUser).where(
                EnterpriseUser.tenant_id == tenant_id,
                EnterpriseUser.primary_email.ilike(email_or_name.strip()),
            )
            res = await conn.execute(stmt)
            user = res.mappings().fetchone()
            if user:
                logger.info("resolve_meeting_speaker.email_resolved", resolved_id=str(user["id"]))
                return user["id"], user["primary_email"], user["display_name"]

        # Display Name lookup
        stmt = select(EnterpriseUser).where(
            EnterpriseUser.tenant_id == tenant_id,
            EnterpriseUser.display_name.ilike(email_or_name.strip()),
        )
        res = await conn.execute(stmt)
        user = res.mappings().fetchone()
        if user:
            logger.info("resolve_meeting_speaker.display_resolved", resolved_id=str(user["id"]))
            return user["id"], user["primary_email"], user["display_name"]

        # ChatUser lookup
        stmt = select(ChatUser).where(
            ChatUser.tenant_id == tenant_id,
            (ChatUser.display_name.ilike(email_or_name.strip()))
            | (ChatUser.email.ilike(email_or_name.strip())),
        )
        res = await conn.execute(stmt)
        chat_user = res.mappings().fetchone()
        if chat_user and chat_user["internal_id"]:
            logger.info(
                "resolve_meeting_speaker.chat_resolved", resolved_id=str(chat_user["internal_id"])
            )
            return chat_user["internal_id"], chat_user["email"], chat_user["display_name"]

        # Fallback
        email = email_or_name if "@" in email_or_name else None
        logger.info("resolve_meeting_speaker.not_resolved", email_or_name=email_or_name)
        return None, email, email_or_name


async def parse_and_chunk_transcript(
    vtt_text: str,
    db_engine: AsyncEngine,
    tenant_id: UUID,
    meeting_id: str,
    meeting_topic: str,
    meeting_start: datetime,
    host_email_or_name: str,
    platform: SourcePlatform,
    duration_seconds: int | None = None,
    meeting_type: str | None = None,
    has_recording: bool = False,
    connection: str | None = None,
) -> list[CanonicalEvent]:
    """Parse WebVTT transcript, resolve identities, group, and return events."""
    cues = parse_vtt(vtt_text)
    if not cues:
        return []

    # Calculate duration
    if duration_seconds is not None:
        duration_sec = int(duration_seconds)
    else:
        duration_sec = int(cues[-1].end_offset) if cues else 0

    # Reject active/ongoing meetings (duration <= 0)
    if duration_sec <= 0:
        return []

    # Resolve speakers
    unique_speakers = {cue.speaker for cue in cues}
    speaker_resolutions = {}
    allowed_identity_ids = []

    for spk in unique_speakers:
        uid, email, dname = await resolve_meeting_speaker(db_engine, tenant_id, spk)
        speaker_resolutions[spk] = {
            "internal_id": str(uid) if uid else None,
            "email": email,
            "display_name": dname,
        }
        if uid:
            allowed_identity_ids.append(uid)

    # Resolve host
    host_uid, host_email, host_dname = await resolve_meeting_speaker(
        db_engine, tenant_id, host_email_or_name
    )
    if host_uid and host_uid not in allowed_identity_ids:
        allowed_identity_ids.append(host_uid)

    author = AuthorInfo(
        internal_id=host_uid,
        email=host_email or "unknown@meeting.local",
        display_name=host_dname,
        source_id=host_email_or_name,
    )

    meeting_metadata = MeetingMetadata(
        duration_seconds=duration_sec,
        attendee_count=max(1, len(unique_speakers)),
        meeting_type=meeting_type,
        has_recording=has_recording,
    )

    chunks = chunk_cues(cues, max_chars=4000)
    total_chunks = len(chunks)
    events = []

    for i, chunk in enumerate(chunks, 1):
        formatted_lines = [format_cue_line(cue) for cue in chunk]
        content_clean = "\n".join(formatted_lines)
        content_raw = f"[Chunk {i} of {total_chunks}]\n" + content_clean

        access_control = AccessControl(
            is_public=False,
            visibility=AccessVisibility.PRIVATE
            if allowed_identity_ids
            else AccessVisibility.PUBLIC,
            acl_type=AclType.ATTENDEES,
            allowed_identity_ids=allowed_identity_ids,
            source_group_id=meeting_id,
        )

        event = CanonicalEvent(
            tenant_id=tenant_id,
            parent_event_id=None,
            source_platform=platform,
            source_type=SourceType.MEETING_TRANSCRIPT,
            event_action=EventAction.CREATED,
            source_id=f"{meeting_id}_chunk_{i}",
            happened_at=meeting_start,
            author=author,
            content_raw=content_raw,
            content_clean=content_clean,
            access_control=access_control,
            signal_status=SignalStatus.UNPROCESSED,
            connection=connection,
            meeting_metadata=meeting_metadata,
            metadata={
                "meeting_id": meeting_id,
                "meeting_topic": meeting_topic,
                "chunk_index": i,
                "total_chunks": total_chunks,
                "speakers": list(speaker_resolutions.values()),
            },
        )
        events.append(event)

    return events

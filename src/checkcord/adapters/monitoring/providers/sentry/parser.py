import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

from checkcord.adapters.monitoring.providers.sentry.schemas import SentryWebhookPayload
from checkcord.models.common import AccessControl, AuthorInfo, EntityReference, IncidentMetadata
from checkcord.models.enums import (
    AccessVisibility,
    AclType,
    EventAction,
    SourcePlatform,
    SourceType,
)
from checkcord.models.event import CanonicalEvent


def clean_file_path(path: str | None) -> str | None:
    if not path:
        return None
    # Strip common container prefixes
    path = re.sub(r"^/app/", "", path)
    path = re.sub(r"^\./", "", path)
    return path


def extract_sentry_entities(payload: SentryWebhookPayload) -> list[EntityReference]:
    entities = []

    # Project/Service entity
    project = payload.project_slug or payload.project or "unknown-project"
    entities.append(
        EntityReference(
            entity_type="service",
            entity_id=project,
            entity_platform="sentry",
            relationship="affected_service",
        )
    )

    # File entities from stacktrace
    seen_files = set()
    if payload.event and payload.event.exception and payload.event.exception.values:
        for val in payload.event.exception.values:
            if val.stacktrace and val.stacktrace.frames:
                for frame in val.stacktrace.frames:
                    if frame.filename and ("site-packages" not in frame.filename):
                        cleaned = clean_file_path(frame.filename)
                        if cleaned and cleaned not in seen_files:
                            seen_files.add(cleaned)
                            entities.append(
                                EntityReference(
                                    entity_type="file",
                                    entity_id=cleaned,
                                    entity_platform="github",
                                    relationship="affected_file",
                                )
                            )
    return entities


def format_sentry_content(payload: SentryWebhookPayload) -> str:
    project = payload.project_name or payload.project_slug or payload.project or "Unknown"
    level = (payload.level or "error").upper()
    title = payload.message or (payload.event.title if payload.event else "Sentry Event")

    lines = [
        f"# Sentry Incident: {project} [{level}]",
        f"**Message:** {title}",
    ]

    if payload.url:
        lines.append(f"**URL:** {payload.url}")

    if payload.event and payload.event.culprit:
        lines.append(f"**Culprit:** {payload.event.culprit}")

    # Stacktrace formatting (cap at 10 frames)
    if payload.event and payload.event.exception and payload.event.exception.values:
        lines.append("\n## Stack Trace")
        for val in payload.event.exception.values:
            if val.stacktrace and val.stacktrace.frames:
                frames_to_show = val.stacktrace.frames[-10:]
                for frame in frames_to_show:
                    filename = clean_file_path(frame.filename) or "unknown"
                    func = frame.function or "unknown"
                    lineno = f":L{frame.lineNo}" if frame.lineNo else ""
                    lines.append(f"- `{filename}{lineno}` in `{func}`")

    return "\n".join(lines)


def parse_sentry_webhook_event(
    payload: SentryWebhookPayload,
    tenant_id: UUID,
) -> CanonicalEvent:
    happened_at = None
    if payload.event and payload.event.timestamp:
        try:
            happened_at = datetime.fromtimestamp(payload.event.timestamp, tz=UTC)
        except Exception:
            pass
    if not happened_at:
        happened_at = datetime.now(UTC)

    content = format_sentry_content(payload)
    entities = extract_sentry_entities(payload)

    project = payload.project_slug or payload.project or "unknown-project"

    incident_metadata = IncidentMetadata(
        severity=payload.level or "error",
        environment="unknown",
        affected_services=[project],
        resolved_at=None,
        root_cause_identified=False,
    )

    acl = AccessControl(
        is_public=False,
        visibility=AccessVisibility.PRIVATE,
        acl_type=AclType.PROJECT_ROLES,
        allowed_identity_ids=[],
    )

    author = AuthorInfo(
        source_id="sentry_system",
        display_name="Sentry",
    )

    event_id = uuid4()
    if payload.event and payload.event.event_id:
        source_id = f"sentry:event:{payload.event.event_id}"
    else:
        source_id = f"sentry:alert:{payload.id or event_id.hex}"

    return CanonicalEvent(
        event_id=event_id,
        tenant_id=tenant_id,
        source_platform=SourcePlatform.SENTRY,
        source_type=SourceType.INCIDENT,
        event_action=EventAction.CREATED,
        source_id=source_id,
        version=1,
        happened_at=happened_at,
        author=author,
        content_raw=content,
        content_clean=content,
        source_raw_payload=payload.model_dump(mode="json"),
        access_control=acl,
        entities=entities,
        incident_metadata=incident_metadata,
        metadata={
            "project": project,
            "level": payload.level,
            "html_url": payload.url,
        },
    )

import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

from checkcord.adapters.monitoring.providers.datadog.schemas import DatadogWebhookPayload
from checkcord.models.common import AccessControl, AuthorInfo, EntityReference, IncidentMetadata
from checkcord.models.enums import (
    AccessVisibility,
    AclType,
    EventAction,
    SourcePlatform,
    SourceType,
)
from checkcord.models.event import CanonicalEvent


def extract_datadog_entities(payload: DatadogWebhookPayload) -> list[EntityReference]:
    entities = []

    # Regex patterns for service extraction
    service_patterns = [
        r"(?:\[service:\s*|service:\s*|service=[\"']?|affected service:\s*)([\w\-]+)",
    ]

    # Find services in title or body
    seen_services = set()
    for text in (payload.title, payload.body):
        for pattern in service_patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                svc = match.group(1).lower()
                if svc not in seen_services:
                    seen_services.add(svc)
                    entities.append(
                        EntityReference(
                            entity_type="service",
                            entity_id=svc,
                            entity_platform="datadog",
                            relationship="affected_service",
                        )
                    )

    # Search for repository/github mentions (e.g. org/repo or github.com/org/repo)
    repo_pattern = r"(?:github\.com/)?([\w\-]+)/([\w\-]+)"
    seen_repos = set()
    for text in (payload.title, payload.body):
        for match in re.finditer(repo_pattern, text):
            repo_full = f"{match.group(1)}/{match.group(2)}"
            # Exclude standard prefixes like service/name or other false positives
            if (
                "/" in repo_full
                and not repo_full.startswith("service/")
                and repo_full not in seen_repos
            ):
                seen_repos.add(repo_full)
                entities.append(
                    EntityReference(
                        entity_type="repo",
                        entity_id=repo_full,
                        entity_platform="github",
                        relationship="affected_repository",
                    )
                )

    return entities


def parse_datadog_webhook_event(
    payload: DatadogWebhookPayload,
    tenant_id: UUID,
) -> CanonicalEvent:
    happened_at = None
    if payload.date:
        try:
            happened_at = datetime.fromtimestamp(payload.date, tz=UTC)
        except Exception:
            pass
    if not happened_at:
        happened_at = datetime.now(UTC)

    content = f"# Datadog Incident: {payload.title}\n\n{payload.body}"
    if payload.url:
        content = f"{content}\n\n**URL:** {payload.url}"

    entities = extract_datadog_entities(payload)

    # Extract env from title/body or default to "unknown"
    env_match = re.search(
        r"(?:env|environment)[:=]\s*([\w\-]+)", f"{payload.title} {payload.body}", re.IGNORECASE
    )
    environment = env_match.group(1).lower() if env_match else "unknown"

    severity = (
        "error"
        if "fail" in payload.title.lower() or "error" in payload.title.lower()
        else "warning"
    )

    affected_services = [e.entity_id for e in entities if e.entity_type == "service"]

    resolved_at = None
    if any(k in payload.title.upper() for k in ("RESOLVED", "RECOVERED", "CLEARED")):
        resolved_at = happened_at

    incident_metadata = IncidentMetadata(
        severity=severity,
        environment=environment,
        affected_services=affected_services,
        resolved_at=resolved_at,
        root_cause_identified=False,
    )

    acl = AccessControl(
        is_public=False,
        visibility=AccessVisibility.PRIVATE,
        acl_type=AclType.PROJECT_ROLES,
        allowed_identity_ids=[],
    )

    author = AuthorInfo(
        source_id="datadog_system",
        display_name="Datadog",
    )

    event_id = uuid4()
    source_id = f"datadog:event:{payload.id}"

    return CanonicalEvent(
        event_id=event_id,
        tenant_id=tenant_id,
        source_platform=SourcePlatform.DATADOG,
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
            "level": severity,
            "html_url": payload.url,
        },
    )

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from checkcord.models.common import AccessControl, AuthorInfo, DocumentMetadata
from checkcord.models.enums import (
    AccessVisibility,
    AclType,
    EventAction,
    SourcePlatform,
    SourceType,
)
from checkcord.models.event import CanonicalEvent


class Author(BaseModel):
    id: str
    name: str | None = None
    email: str | None = None


class Audience(BaseModel):
    public: bool = False
    emails: list[str] = Field(default_factory=list, max_length=1000)
    identity_ids: list[UUID] = Field(default_factory=list, max_length=1000)


class SignedWebhookEvent(BaseModel):
    """One item sent to a signed webhook connection."""

    id: str = Field(min_length=1, max_length=500, description="Stable id in the sending system")
    text: str = Field(min_length=1)
    title: str | None = None
    url: str | None = None
    container: str | None = Field(
        default=None, max_length=500, description="Channel, project or folder; what scope matches"
    )
    action: EventAction = EventAction.CREATED
    happened_at: datetime | None = None
    author: Author | None = None
    audience: Audience = Field(default_factory=Audience)


def to_event(
    item: SignedWebhookEvent, tenant_id: UUID, connection: str, readers: list[UUID]
) -> CanonicalEvent:
    """``readers`` are the people the audience resolved to; an empty private audience means
    no person-scoped read returns the event."""
    public = item.audience.public
    author = item.author or Author(id=item.id)
    return CanonicalEvent(
        tenant_id=tenant_id,
        source_platform=SourcePlatform.CUSTOM,
        source_type=SourceType.CUSTOM_PAYLOAD,
        event_action=item.action,
        source_id=item.id,
        happened_at=item.happened_at or datetime.now(UTC),
        author=AuthorInfo(
            source_id=author.id, display_name=author.name or author.id, email=author.email
        ),
        content_raw=item.text,
        content_clean=item.text,
        source_raw_payload=item.model_dump(mode="json", exclude={"text"}),
        access_control=AccessControl(
            is_public=public,
            visibility=AccessVisibility.PUBLIC if public else AccessVisibility.PRIVATE,
            acl_type=AclType.OPEN if public else AclType.CUSTOM,
            allowed_identity_ids=[] if public else readers,
            source_group_id=item.container,
        ),
        connection=connection,
        document_metadata=(
            DocumentMetadata(title=item.title, url=item.url or "") if item.title else None
        ),
    )

import uuid

from checkcord.core.config import Settings


def tenant_for_new_installation(settings: Settings) -> uuid.UUID:
    return settings.default_tenant_id or uuid.uuid4()

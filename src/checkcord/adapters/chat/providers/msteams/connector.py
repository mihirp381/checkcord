from checkcord.adapters.chat.providers.msteams import directory_sync
from checkcord.adapters.chat.providers.msteams.listeners import router as listeners_router
from checkcord.adapters.chat.providers.msteams.oauth import router
from checkcord.connectors.base import Connector, OAuthApp, Poll
from checkcord.storage.models import MSTeamsInstallation

CONNECTORS = (
    Connector(
        name="teams",
        title="Microsoft Teams",
        ingestion="Bot Framework messages",
        status="beta",
        scope="Teams team or channel ids",
        audience_platform="ms_teams",
        audience_from="source",
        table=MSTeamsInstallation,
        identifier="ms_tenant_id",
        routers=((router, "install"), (listeners_router, "webhooks")),
        poll=Poll(
            "sync_msteams_directory",
            directory_sync.run_scheduled,
            lambda s: s.directory_sync_interval_minutes,
            publishes=False,
        ),
        oauth=OAuthApp("msteams_client_id", "msteams_redirect_uri"),
    ),
)

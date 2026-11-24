from checkcord.adapters.tasks.providers.linear.router import poll_linear_organizations, router
from checkcord.connectors.base import Connector, OAuthApp, Poll
from checkcord.storage.models import LinearInstallation

CONNECTORS = (
    Connector(
        name="linear",
        title="Linear",
        ingestion="poller",
        status="experimental",
        audience_from="admin",
        scope="Linear team ids or keys",
        table=LinearInstallation,
        identifier="linear_organization_id",
        secret_columns=("access_token", "webhook_secret"),
        routers=((router, "install"),),
        poll=Poll(
            "poll_linear_organizations",
            poll_linear_organizations,
            lambda s: s.task_document_poll_interval_minutes,
        ),
        oauth=OAuthApp("linear_client_id", "linear_redirect_uri"),
    ),
)

from checkcord.adapters.tasks.providers.notion.router import poll_notion_workspaces, router
from checkcord.connectors.base import Connector, OAuthApp, Poll
from checkcord.storage.models import NotionInstallation

CONNECTORS = (
    Connector(
        name="notion",
        title="Notion",
        ingestion="poller",
        status="experimental",
        audience_from="admin",
        scope="Notion database or page ids",
        table=NotionInstallation,
        identifier="notion_workspace_id",
        secret_columns=("access_token", "webhook_secret"),
        routers=((router, "install"),),
        poll=Poll(
            "poll_notion_workspaces",
            poll_notion_workspaces,
            lambda s: s.task_document_poll_interval_minutes,
        ),
        oauth=OAuthApp("notion_client_id", "notion_redirect_uri"),
    ),
)

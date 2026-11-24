"""One HTTP and WebSocket server holding every stand-in provider."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiohttp import web

from sim.world import control
from sim.world.github import GitHub
from sim.world.idp import FakeIdP
from sim.world.jira import Jira
from sim.world.linear import Linear
from sim.world.model_proxy import ModelProxy
from sim.world.notion import Notion
from sim.world.slack import Slack
from sim.world.state import Deliveries, Log
from sim.world.teams import Teams
from sim.world.zoom import Zoom

DEFAULT_PORT = 8900
IDP_PREFIX = "/idp"


@dataclass(frozen=True)
class Config:
    port: int
    public_base: str
    checkcord_url: str
    slack_client_id: str
    slack_client_secret: str
    slack_app_token: str
    github_token: str
    jira_token: str
    oidc_client_id: str
    oidc_client_secret: str
    notion_client_id: str = "sim-notion"
    notion_client_secret: str = "sim-notion-secret"
    linear_client_id: str = "sim-linear"
    linear_client_secret: str = "sim-linear-secret"
    teams_client_id: str = "sim-teams"
    teams_client_secret: str = "sim-teams-secret"
    model_upstream: str = "http://localhost:11434/v1"
    bind: str = "127.0.0.1"

    @property
    def jira_host(self) -> str:
        return self.public_base.split("://", 1)[1]

    @property
    def issuer(self) -> str:
        return self.public_base + IDP_PREFIX

    @classmethod
    def from_env(cls) -> Config:
        port = int(os.environ.get("SIM_WORLD_PORT", DEFAULT_PORT))
        env = os.environ
        return cls(
            port=port,
            public_base=env.get("SIM_WORLD_PUBLIC", f"http://host.docker.internal:{port}"),
            checkcord_url=env.get("SIM_CHECKCORD_URL", "http://localhost:8410"),
            slack_client_id=env.get("SIM_SLACK_CLIENT_ID", "sim-client"),
            slack_client_secret=env.get("SIM_SLACK_CLIENT_SECRET", "sim-secret"),
            slack_app_token=env.get("SIM_SLACK_APP_TOKEN", "xapp-sim"),
            github_token=env.get("SIM_GITHUB_TOKEN", "ghp-sim"),
            jira_token=env.get("SIM_JIRA_TOKEN", "jira-sim"),
            oidc_client_id=env.get("SIM_OIDC_CLIENT_ID", "checkcord-sim"),
            oidc_client_secret=env.get("SIM_OIDC_CLIENT_SECRET", "oidc-sim"),
            notion_client_id=env.get("SIM_NOTION_CLIENT_ID", "sim-notion"),
            notion_client_secret=env.get("SIM_NOTION_CLIENT_SECRET", "sim-notion-secret"),
            linear_client_id=env.get("SIM_LINEAR_CLIENT_ID", "sim-linear"),
            linear_client_secret=env.get("SIM_LINEAR_CLIENT_SECRET", "sim-linear-secret"),
            teams_client_id=env.get("SIM_TEAMS_CLIENT_ID", "sim-teams"),
            teams_client_secret=env.get("SIM_TEAMS_CLIENT_SECRET", "sim-teams-secret"),
            model_upstream=env.get("SIM_MODEL_UPSTREAM", "http://localhost:11434/v1"),
            bind=env.get("SIM_WORLD_BIND", "127.0.0.1"),
        )


def service_of(path: str) -> str | None:
    if path.startswith("/control"):
        return None
    if path.startswith("/github"):
        return "github"
    if path.startswith("/rest/"):
        return "jira"
    if path.startswith(IDP_PREFIX):
        return "idp"
    if path.startswith("/model"):
        return "model"
    for name in ("zoom", "notion", "linear", "teams"):
        if path.startswith(f"/{name}/"):
            return name
    return "slack"


class World:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.log = Log()
        self.deliveries = Deliveries()
        self.slack = Slack(
            config.public_base,
            config.slack_client_id,
            config.slack_client_secret,
            config.slack_app_token,
        )
        self.github = GitHub(config.github_token, self.deliveries, config.checkcord_url)
        self.jira = Jira(config.jira_host, config.jira_token, self.deliveries)
        self.idp = FakeIdP(
            issuer=config.issuer,
            client_id=config.oidc_client_id,
            client_secret=config.oidc_client_secret,
        )
        self.model = ModelProxy(config.model_upstream)
        self.zoom = Zoom(config.public_base, self.deliveries)
        self.notion = Notion(config.notion_client_id, config.notion_client_secret)
        self.linear = Linear(config.linear_client_id, config.linear_client_secret)
        self.teams = Teams(
            config.public_base,
            config.checkcord_url,
            config.teams_client_id,
            config.teams_client_secret,
            self.deliveries,
        )

    def app(self) -> web.Application:
        @web.middleware
        async def record(
            request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
        ) -> web.StreamResponse:
            service = service_of(request.path)
            websocket = request.headers.get("Upgrade", "").lower() == "websocket"
            raw = b"" if websocket or service is None else await request.read()
            status = 500
            try:
                response = await handler(request)
                status = response.status
                return response
            except web.HTTPException as err:
                status = err.status
                raise
            finally:
                if service is not None:
                    self.log.add(
                        service, request.method, request.path, request.query_string, raw, status
                    )

        app = web.Application(middlewares=[record], client_max_size=32 * 1024 * 1024)
        self.slack.routes(app)
        self.github.routes(app)
        self.jira.routes(app)
        self.idp.routes(app, IDP_PREFIX)
        self.model.routes(app)
        self.zoom.routes(app)
        self.notion.routes(app)
        self.linear.routes(app)
        self.teams.routes(app)
        control.routes(app, self)

        async def close(_: web.Application) -> None:
            await self.model.close()

        app.on_cleanup.append(close)
        return app


def serve(config: Config) -> None:
    web.run_app(World(config).app(), host=config.bind, port=config.port, access_log=None)

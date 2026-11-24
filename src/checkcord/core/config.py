import json
from functools import lru_cache
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from checkcord.core.embedding_models import DEFAULT_EMBEDDING_MODELS

SLACK_BOT_SCOPES = (
    "channels:history",
    "groups:history",
    "channels:read",
    "groups:read",
    "users:read",
    "users:read.email",
    "usergroups:read",
)
PROVIDER_ADDRESSES = (
    "github_api_url",
    "slack_api_url",
    "msgraph_url",
    "ms_login_url",
    "botframework_openid_url",
    "linear_api_url",
    "linear_oauth_url",
    "notion_api_url",
)
SLACK_BOT_EVENTS = (
    "message.channels",
    "message.groups",
    "member_joined_channel",
    "member_left_channel",
    "channel_converted_to_private",
    "app_uninstalled",
    "tokens_revoked",
)


class Settings(BaseSettings):
    """Settings read from the environment."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Environment
    environment: str = Field(default="development")
    log_level: str = Field(default="INFO", description="Application log level")
    audit_log_lines: bool = Field(
        default=True,
        description=(
            "Also write each audit entry as one JSON log line on the checkcord.audit logger."
        ),
    )
    fernet_master_key: str | None = Field(
        default=None,
        description="Base64 32-byte Fernet key for token encryption",
    )
    oauth_state_secret: str | None = Field(
        default=None,
        description="HMAC key for OAuth install links; derived from FERNET_MASTER_KEY if unset.",
    )

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://checkcord:checkcord@localhost:5432/checkcord",
        description="Postgres connection string",
    )
    migration_database_url: str | None = Field(
        default=None,
        description="Connection string for the schema-owner role; falls back to database_url.",
    )

    # Redis
    redis_url: str = Field(
        default="redis://localhost:6379/0", description="Redis connection string"
    )

    # Redis Stream Settings
    redis_stream_batch_size: int = Field(
        default=10, description="Max messages to pull per XREADGROUP"
    )
    redis_stream_poll_timeout_ms: int = Field(
        default=1000, description="Block time for XREADGROUP in ms"
    )
    worker_sweep_interval_seconds: int = Field(
        default=60,
        description=(
            "How often the worker retries unfinished events and trims stream entries "
            "that every consumer group has acknowledged."
        ),
    )
    redis_stream_reclaim_interval_ms: int = Field(
        default=30_000,
        description=(
            "How often each consumer runs an XAUTOCLAIM sweep for its own "
            "group, independent of the XREADGROUP poll cadence."
        ),
    )
    redis_stream_reclaim_min_idle_ms: int = Field(
        default=120_000,
        description=(
            "Milliseconds a pending message must sit unacknowledged before "
            "another consumer may reclaim it; keep it above the longest handler "
            "runtime."
        ),
    )

    # Slack OAuth Configuration
    slack_client_id: str | None = Field(default=None, description="Slack OAuth client id")
    slack_client_secret: str | None = Field(default=None, description="Slack OAuth client secret")
    slack_signing_secret: str | None = Field(default=None, description="Slack signing secret")
    slack_redirect_uri: str | None = Field(default=None, description="Slack OAuth redirect URI")
    slack_app_token: str | None = Field(default=None, description="Slack Socket Mode app token")
    slack_bot_scopes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(SLACK_BOT_SCOPES),
        description="Bot scopes requested at install, comma-separated or a JSON list.",
    )

    # MS Teams OAuth Configuration
    msteams_client_id: str | None = Field(default=None, description="Microsoft Teams app client id")
    msteams_client_secret: str | None = Field(
        default=None, description="Microsoft Teams app client secret"
    )
    msteams_redirect_uri: str | None = Field(
        default=None, description="Microsoft Teams redirect URI"
    )

    # Notion OAuth Configuration
    notion_client_id: str | None = Field(default=None, description="Notion OAuth client id")
    notion_client_secret: str | None = Field(default=None, description="Notion OAuth client secret")
    notion_redirect_uri: str | None = Field(default=None, description="Notion OAuth redirect URI")

    # Linear OAuth Configuration
    linear_client_id: str | None = Field(default=None, description="Linear OAuth client id")
    linear_client_secret: str | None = Field(default=None, description="Linear OAuth client secret")
    linear_redirect_uri: str | None = Field(default=None, description="Linear OAuth redirect URI")

    # Provider addresses
    github_api_url: str = Field(
        default="https://api.github.com", description="GitHub REST API base"
    )
    slack_api_url: str = Field(
        default="https://slack.com/api/", description="Slack Web API base, ending in /api/"
    )
    msgraph_url: str = Field(
        default="https://graph.microsoft.com", description="Microsoft Graph origin"
    )
    ms_login_url: str = Field(
        default="https://login.microsoftonline.com", description="Microsoft Entra sign-in origin"
    )
    botframework_openid_url: str = Field(
        default="https://login.botframework.com/v1/.well-known/openidconfiguration",
        description="Bot Framework OpenID metadata, for verifying Teams bot tokens",
    )
    linear_api_url: str = Field(default="https://api.linear.app", description="Linear API origin")
    linear_oauth_url: str = Field(
        default="https://linear.app", description="Linear origin that serves the OAuth consent page"
    )
    notion_api_url: str = Field(default="https://api.notion.com", description="Notion API origin")

    task_document_poll_interval_minutes: int = Field(
        default=15,
        description="Interval in minutes between Notion/Linear/Jira document poller sweeps",
    )
    github_poll_interval_minutes: int = Field(
        default=10, ge=1, description="Interval in minutes between GitHub event poller sweeps"
    )

    # Single-organization tenancy
    default_tenant_id: UUID | None = Field(
        default=None,
        description="Tenant that new Slack and MS Teams installations join instead of a new one.",
    )

    # LLM provider
    openai_api_key: str | None = Field(
        default=None, description="API key for chat models and OpenAI embeddings."
    )
    llm_base_url: str | None = Field(
        default=None, description="Base URL of an OpenAI-compatible endpoint for chat models."
    )
    llm_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        description="Seconds one chat-model request may take, linkage verdicts included.",
    )

    # Embeddings Configuration
    embedding_provider: Literal["openai", "voyage"] = Field(
        default="voyage",
        description=(
            "Embeddings backend for ingestion and query: 'voyage' or 'openai'. "
            "Both sides must use the same provider, model and dimension; changing "
            "any of them requires re-embedding every record."
        ),
    )
    voyage_api_key: str | None = Field(
        default=None, description="Voyage AI API key, used when embedding_provider='voyage'"
    )
    embedding_model: str = Field(
        default="voyage-code-3",
        description=(
            "Embedding model id. Defaults to 'voyage-code-3' for voyage and "
            "'text-embedding-3-small' for openai."
        ),
    )
    embedding_dimension: int = Field(
        default=1024,
        description=(
            "Vector size requested from the embedding model. Must equal the "
            "pgvector column dimension or every similarity query fails."
        ),
    )
    embedding_base_url: str | None = Field(
        default=None,
        description="OpenAI-compatible embeddings endpoint, used when embedding_provider=openai.",
    )
    embedding_send_dimensions: bool | None = Field(
        default=None,
        description=(
            "Ask the OpenAI-style endpoint for embedding_dimension-wide vectors. Unset: only for "
            "text-embedding-3 models."
        ),
    )
    confidence_aggregator_interval_minutes: int = Field(
        default=60, description="Interval in minutes for background confidence score aggregation"
    )

    directory_sync_interval_minutes: int = Field(
        default=60,
        ge=1,
        description=(
            "How often the Slack and Teams user directories are read again and their accounts "
            "matched to people by email. A sync also runs once after each install."
        ),
    )

    # Audiences of Slack channels, Teams conversations and GitHub repositories
    audience_refresh_interval_minutes: int = Field(
        default=60,
        ge=1,
        description=(
            "How often each stored channel or repository audience is read again from its "
            "provider. Ingestion reuses a stored audience read within this interval."
        ),
    )
    audience_refresh_grace_hours: int = Field(
        default=24,
        ge=0,
        description=(
            "How long a stored audience is kept while its provider cannot be read; after "
            "that it is empty until a read succeeds."
        ),
    )
    audience_refresh_batch_size: int = Field(
        default=500,
        ge=1,
        description="Events updated per transaction when a large container's audience changes.",
    )

    review_new_connections: bool = Field(
        default=False,
        description=(
            "Start every new connection in review: its events and records are held until an "
            "admin releases them."
        ),
    )

    # Retention
    event_retention_days: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Days to keep events, counted from when they happened. A daily job deletes older "
            "events and the records made only from them. Unset keeps events forever."
        ),
    )
    audit_retention_days: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Days to keep audit entries. `checkcord audit prune` deletes older ones on the "
            "migration connection. Unset keeps them forever."
        ),
    )

    # Scrubber Configuration
    scrubber_llm_model: str = Field(
        default="gpt-4o-mini", description="LLM model for binary chat scrubbing"
    )

    linkage_llm_model: str = Field(
        default="gpt-4o-mini", description="LLM model for linkage adjudication."
    )
    pr_check_model: str = Field(
        default="gpt-4o-mini",
        description="LLM model that judges a pull request's diff against each recorded decision.",
    )
    pr_check_on_error: Literal["error", "success"] = Field(
        default="error",
        description="Status when no verdict could be reached after retries: error holds the "
        "merge; success lets it through with the description 'Not evaluated: internal error.'",
    )
    pr_check_mode: Literal["judge", "touch"] = Field(
        default="judge",
        description="judge: a model reads the diff against each record; touch: no model call, "
        "the check lists records attached to changed code.",
    )
    skill_synthesis_llm_model: str = Field(
        default="gpt-4o", description="LLM model for skill synthesis from agent trajectories."
    )

    chat_buffer_max_messages: int = Field(
        default=5, description="Top-level messages a channel buffer holds before it is assigned"
    )
    chat_buffer_timeout_seconds: int = Field(
        default=180, description="Seconds after which a channel buffer is assigned anyway"
    )
    chat_discussion_idle_seconds: int = Field(
        default=1800, description="Seconds without a new message before a discussion is extracted"
    )
    chat_router_model: str = Field(
        default="gpt-4o-mini", description="LLM model that assigns messages to discussions"
    )
    chat_extraction_model: str = Field(
        default="gpt-4o", description="LLM model that reports what a discussion settled"
    )
    chat_settled_model: str = Field(
        default="gpt-4o",
        description="LLM model that judges whether a discussion settled each extracted outcome",
    )

    # Meeting Transcript Extraction Configuration
    meeting_extraction_llm_model: str = Field(
        default="gpt-4o",
        description="model for the meeting-transcript extractors and verifier",
    )
    meeting_judge_mode: Literal["direct", "batch"] = Field(
        default="direct",
        description=(
            "direct: each meeting extraction is verified by the verifier model in the transcript "
            "worker; batch: verified through OpenAI's Batch API by the verifier batch sweep"
        ),
    )
    judge_batch_sweep_interval_minutes: int = Field(
        default=30,
        description="Interval in minutes between JudgeBatchWorker sweeps for pending extractions",
    )

    # PII redaction
    redaction_llm_model: str = Field(
        default="gpt-4o-mini",
        description="Small, cheap LLM model for PII entity detection when a record is stored",
    )
    redaction_model_pass: bool = Field(
        default=True,
        description="Run the model pass when storing redacted text; the regex pass always runs",
    )
    redaction_sweep_batch_size: int = Field(
        default=50,
        ge=1,
        description="Records whose model pass failed that one worker sweep retries",
    )

    # Model usage
    model_prices: dict[str, tuple[float, float]] = Field(
        default_factory=dict,
        description=(
            "Price per million input and output tokens by model, as JSON, e.g. "
            '{"gpt-4o": [2.5, 10]}. When set, usage reports show cost estimates.'
        ),
    )

    # AST backfill and CI/CD ingestion
    ci_failure_extraction_llm_model: str = Field(
        default="gpt-4o",
        description=("LLM model for the CI-failure extractor."),
    )
    repo_backfill_batch_size: int = Field(
        default=200,
        description=(
            "Files processed per batch by the repository backfill before its "
            "resumable cursor advances."
        ),
    )
    ci_log_excerpt_max_lines: int = Field(
        default=500,
        description=(
            "Maximum lines of a CI job log kept when a failed run has no check annotations."
        ),
    )
    search_min_similarity: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Lowest cosine similarity a search_conceptual_context result may have.",
    )
    personalization_enabled: bool = Field(
        default=False,
        description=(
            "Whether search_conceptual_context ranks records from repositories the caller "
            "works in ahead of organization-wide ones."
        ),
    )
    # Reranking and golden evaluation
    rerank_enabled: bool = Field(
        default=False,
        description=(
            "Whether the clustering worker reranks its dedup candidates with the Voyage reranker."
        ),
    )
    rerank_model: str = Field(
        default="rerank-2.5",
        description="Voyage AI rerank model id used by pipeline/reranker.py::VoyageReranker.",
    )
    rerank_candidate_pool_size: int = Field(
        default=20,
        description=(
            "Maximum candidates fetched and handed to the reranker. Only applied "
            "when rerank_enabled is true."
        ),
    )
    rerank_timeout_seconds: float = Field(
        default=10.0,
        description=(
            "Timeout in seconds for one rerank call. On timeout or failure the "
            "pre-rerank order is used."
        ),
    )

    # Web viewer
    public_base_url: str | None = Field(
        default=None,
        description="Public URL of the API, used to print webhook URLs.",
    )
    web_base_url: str = Field(
        default="http://localhost:3000",
        description=(
            "Base URL of the web viewer, used to build links in PR comments. Set it per deployment."
        ),
    )
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="CORS origins, comma-separated or a JSON list; defaults to web_base_url.",
    )
    api_docs_enabled: bool | None = Field(
        default=None,
        description="Serve /docs and /openapi.json. Unset: on except in production.",
    )
    forwarded_allow_ips: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["127.0.0.1"],
        description="Proxies whose X-Forwarded-For header gives the client address.",
    )

    # Metrics and health
    metrics_enabled: bool = Field(
        default=False, description="Serve Prometheus metrics at /metrics on the API and worker."
    )
    metrics_token: str | None = Field(
        default=None, description="When set, /metrics needs this bearer token."
    )
    worker_http_host: str = Field(
        default="127.0.0.1", description="Address of the worker's health and metrics server."
    )
    worker_http_port: int = Field(
        default=9100, ge=0, description="Port of the worker's health and metrics server; 0: off."
    )

    # Rate limits, counted per minute in Redis
    rate_limit_enabled: bool = Field(default=True, description="Apply the request limits.")
    rate_limit_requests_per_minute: int = Field(
        default=600, ge=1, description="Requests per API key or session."
    )
    rate_limit_auth_failures_per_minute: int = Field(
        default=30, ge=1, description="Failed authentications per client address."
    )
    rate_limit_sign_in_per_minute: int = Field(
        default=30, ge=1, description="Sign-in requests per client address."
    )
    rate_limit_webhooks_per_minute: int = Field(
        default=1200, ge=1, description="Verified provider webhooks per connection."
    )

    # Browser sign-in (OpenID Connect)
    oidc_issuer: str | None = Field(
        default=None, description="Issuer URL of the identity provider; unset turns sign-in off."
    )
    oidc_client_id: str | None = Field(default=None, description="Client id at the provider.")
    oidc_client_secret: str | None = Field(
        default=None, description="Client secret; unset for a public client using PKCE only."
    )
    oidc_redirect_uri: str | None = Field(
        default=None,
        description="Callback registered at the provider. Default: PUBLIC_BASE_URL + "
        "/api/v1/auth/callback.",
    )
    oidc_scopes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["openid", "email", "profile"],
        description="Scopes requested at sign-in, space- or comma-separated.",
    )
    oidc_allow_missing_email_verified: bool = Field(
        default=False,
        description="Accept an email claim without email_verified. An explicit false is refused.",
    )
    oidc_auto_create_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Email domains whose unknown people are created at first sign-in.",
    )
    oidc_admin_groups_claim: str | None = Field(
        default=None,
        description="ID token claim listing the person's groups; set it to manage admin there.",
    )
    oidc_admin_groups: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Values of the groups claim that make a person an admin.",
    )
    scim_admin_groups: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description=(
            "SCIM group names whose members are admins. When set, it decides the admin flag "
            "and the sign-in groups claim does not."
        ),
    )
    session_idle_minutes: int = Field(
        default=60, ge=1, description="A session ends after this long without a request."
    )
    session_max_hours: int = Field(
        default=12, ge=1, description="A session ends this long after sign-in in any case."
    )

    @field_validator("oidc_scopes", mode="before")
    @classmethod
    def _parse_scopes(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            if value.startswith("["):
                return json.loads(value)
            return value.replace(",", " ").split()
        return value

    @field_validator(
        "cors_allowed_origins",
        "slack_bot_scopes",
        "oidc_auto_create_domains",
        "oidc_admin_groups",
        "scim_admin_groups",
        "forwarded_allow_ips",
        mode="before",
    )
    @classmethod
    def _parse_list(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            if value.startswith("["):
                return json.loads(value)
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator(*PROVIDER_ADDRESSES)
    @classmethod
    def _provider_address(cls, value: str, info: Any) -> str:
        value = value.strip().rstrip("/")
        return value + "/" if info.field_name == "slack_api_url" else value

    @model_validator(mode="after")
    def _default_origins(self) -> Self:
        if not self.cors_allowed_origins:
            self.cors_allowed_origins = [self.web_base_url]
        return self

    @model_validator(mode="after")
    def _default_settled_model(self) -> Self:
        if "chat_settled_model" not in self.model_fields_set:
            self.chat_settled_model = self.chat_extraction_model
        return self

    @model_validator(mode="after")
    def _default_embedding_model(self) -> Self:
        if "embedding_model" not in self.model_fields_set:
            self.embedding_model = DEFAULT_EMBEDDING_MODELS[self.embedding_provider]
        return self

    @property
    def graph_api(self) -> str:
        return f"{self.msgraph_url}/v1.0"

    @property
    def graph_scope(self) -> str:
        return f"{self.msgraph_url}/.default"

    @property
    def effective_migration_database_url(self) -> str:
        return self.migration_database_url or self.database_url

    @property
    def sign_in_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id)

    @property
    def effective_oidc_redirect_uri(self) -> str | None:
        if self.oidc_redirect_uri:
            return self.oidc_redirect_uri
        if self.public_base_url:
            return self.public_base_url.rstrip("/") + "/api/v1/auth/callback"
        return None

    @property
    def docs_enabled(self) -> bool:
        if self.api_docs_enabled is not None:
            return self.api_docs_enabled
        return self.environment != "production"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()

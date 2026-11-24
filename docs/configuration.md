# Configuration

Checkcord reads its settings from environment variables. With Docker Compose, put them in `.env`; [`.env.example`](../.env.example) lists the ones most deployments set. Restart the `api` and `worker` services after a change.

Run `checkcord doctor` after editing. It checks the settings that can stop Checkcord from working.

## Required

| Variable | Default | Purpose |
|---|---|---|
| `ENVIRONMENT` | `development` | Set to `production` for any real deployment. Production mode refuses to start without an encryption key, with a database role that bypasses row-level security, or with a default database password |
| `FERNET_MASTER_KEY` | none | Encrypts the credentials of connected systems. Required in production |
| `DEFAULT_TENANT_ID` | none | The organization that `checkcord` commands act on, and that people sign in to. Any fixed UUID; not a secret |
| `OPENAI_API_KEY` | none | Key for the chat model endpoint. The worker will not start without it |
| `VOYAGE_API_KEY` | none | Key for embeddings when `EMBEDDING_PROVIDER=voyage` |
| `PUBLIC_BASE_URL` | none | The address connected systems use to reach the API. Printed in webhook and install instructions |

## Database and queue

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | local development database | Connection for the API and worker. Use the `checkcord_app` role |
| `MIGRATION_DATABASE_URL` | same as `DATABASE_URL` | Connection for `checkcord migrate`. Use the `checkcord_owner` role |
| `REDIS_URL` | `redis://localhost:6379/0` | Event queue |
| `POSTGRES_PASSWORD`, `CHECKCORD_OWNER_DB_PASSWORD`, `CHECKCORD_APP_DB_PASSWORD` | none | Compose only. Compose builds the two URLs above from these |

## Models

Checkcord calls a chat model to filter, extract, merge and redact, and an embeddings model for search.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_BASE_URL` | OpenAI | An OpenAI-compatible chat endpoint to use instead, such as Ollama or vLLM. See [local models](self-hosting.md#local-models) |
| `LLM_TIMEOUT_SECONDS` | `60` | How long one chat-model call may take, linking verdicts included. Raise it for slow local models |
| `EMBEDDING_PROVIDER` | `voyage` | `voyage` or `openai` |
| `EMBEDDING_MODEL` | `voyage-code-3` for `voyage`, `text-embedding-3-small` for `openai` | Embeddings model name |
| `EMBEDDING_DIMENSION` | `1024` | Width of the model's vectors. `checkcord migrate` sizes the database column to it. The API and worker refuse to start when the column, the setting or the model disagree. See [changing the embedding model](self-hosting.md#changing-the-embedding-model) |
| `EMBEDDING_BASE_URL` | OpenAI | An OpenAI-compatible embeddings endpoint, used when `EMBEDDING_PROVIDER=openai` |
| `EMBEDDING_SEND_DIMENSIONS` | unset | Whether to ask an OpenAI-style endpoint for `EMBEDDING_DIMENSION`-wide vectors. Unset, Checkcord asks only for `text-embedding-3` models. Set it to `true` if your own endpoint's model can shorten its vectors, `false` if it rejects the parameter |
| `RERANK_ENABLED` | `false` | Reorder merge candidates with a Voyage reranker |
| `RERANK_MODEL` | `rerank-2.5` | Reranker model name |
| `RERANK_CANDIDATE_POOL_SIZE` | `20` | Candidates passed to the reranker |
| `RERANK_TIMEOUT_SECONDS` | `10` | Reranker call timeout |

Each stage has its own model setting, so you can use a cheaper model for high-volume stages. If your endpoint serves different model names, set all of them.

| Variable | Default | Stage |
|---|---|---|
| `SCRUBBER_LLM_MODEL` | `gpt-4o-mini` | Drops noise before anything else runs |
| `CHAT_ROUTER_MODEL` | `gpt-4o-mini` | Assigns chat messages that are not replies to discussions |
| `CHAT_EXTRACTION_MODEL` | `gpt-4o` | Reports the decisions, constraints, requirements and risks a finished discussion settled |
| `CHAT_SETTLED_MODEL` | `CHAT_EXTRACTION_MODEL` | Judges, for each extracted outcome and each meeting decision or constraint, whether the discussion settled it or left it open; open ones are dropped |
| `MEETING_EXTRACTION_LLM_MODEL` | `gpt-4o` | Extracts decisions, constraints and risks from transcripts |
| `MEETING_JUDGE_MODE` | `direct` | How each extraction from a transcript is checked before it becomes a record. `direct`: the transcript worker asks `MEETING_EXTRACTION_LLM_MODEL` at once, on any provider. `batch`: the extractions wait for OpenAI's Batch API, swept every `JUDGE_BATCH_SWEEP_INTERVAL_MINUTES`; it needs OpenAI itself, and `checkcord doctor` fails it on any other `LLM_BASE_URL` |
| `CI_FAILURE_EXTRACTION_LLM_MODEL` | `gpt-4o` | Summarizes CI failures |
| `LINKAGE_LLM_MODEL` | `gpt-4o-mini` | Decides whether an event belongs to an existing record |
| `SKILL_SYNTHESIS_LLM_MODEL` | `gpt-4o` | Derives reusable instructions from agent sessions |
| `REDACTION_LLM_MODEL` | `gpt-4o-mini` | Finds personal data in a record summary when it is stored |
| `PR_CHECK_MODEL` | `gpt-4o-mini` | Judges a pull request's diff against each candidate record, once per record and push |

| Variable | Default | Purpose |
|---|---|---|
| `REDACTION_MODEL_PASS` | `true` | Run the model pass when a record summary is stored. The pattern pass always runs. Reads never call a model |
| `PR_CHECK_ON_ERROR` | `error` | Status when no verdict could be reached after three attempts. `error` holds a required check; `success` lets the change through with the description "Not evaluated: internal error." |
| `PR_CHECK_MODE` | `judge` | `judge`: a model reads the diff against each blocking record in scope and each record attached to changed code, and the check fails only on a conflict with a blocking one. `touch`: no model call; the comment lists records attached to changed code and a blocking one fails the check. See [security.md](security.md#what-is-not-enforced) |
| `REDACTION_SWEEP_BATCH_SIZE` | `50` | Records whose model pass failed that each worker sweep retries |
| `MODEL_PRICES` | unset | Price per million input and output tokens by model, as JSON, for example `{"gpt-4o-mini": [0.15, 0.6]}`. When set, `checkcord usage` shows estimated cost |

Changing `REDACTION_LLM_MODEL` or `REDACTION_MODEL_PASS` changes the redaction version. Run `checkcord redaction recompute` to bring stored copies up to it.

`checkcord doctor` reports a model that cannot return 1024-wide vectors. Changing the embeddings provider, model or dimension makes existing vectors meaningless. Re-embed with `scripts/reembed_canonical_contexts.py`.

## Connectors

Set these only for the systems you connect. Everything else about a connector is stored by `checkcord connect`. See [connectors.md](connectors.md).

| Variable | Purpose |
|---|---|
| `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_SIGNING_SECRET`, `SLACK_REDIRECT_URI` | Your Slack app |
| `SLACK_APP_TOKEN` | Slack Socket Mode token. Checkcord receives Slack events only this way |
| `SLACK_BOT_SCOPES` | Bot scopes requested at install. The default is what Checkcord needs |
| `MSTEAMS_CLIENT_ID`, `MSTEAMS_CLIENT_SECRET`, `MSTEAMS_REDIRECT_URI` | Your Teams bot and Graph app |
| `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET`, `NOTION_REDIRECT_URI` | Your Notion integration |
| `LINEAR_CLIENT_ID`, `LINEAR_CLIENT_SECRET`, `LINEAR_REDIRECT_URI` | Your Linear application |
| `OAUTH_STATE_SECRET` | Signs install links. Derived from `FERNET_MASTER_KEY` when unset |
| `TASK_DOCUMENT_POLL_INTERVAL_MINUTES` | How often Jira, Notion and Linear are polled. Default 15. Each poll reads up to 20 pages per connection and continues where it stopped on the next poll; the first poll reads the last 7 days |
| `GITHUB_POLL_INTERVAL_MINUTES` | How often each connected GitHub repository's events are polled, as a backstop for missed webhooks. Default 10 |
| `DIRECTORY_SYNC_INTERVAL_MINUTES` | How often the Slack and Teams directories are read to match accounts to people by email. Default 60. A directory is also read once after each install, and an account not seen yet is looked up when a channel's audience is read |
| `AUDIENCE_REFRESH_INTERVAL_MINUTES` | How often each Slack channel, Teams conversation and GitHub repository audience is read again. Default 60. Each read costs one or more provider API calls per container, so keep it well inside your rate limits |
| `AUDIENCE_REFRESH_GRACE_HOURS` | How long the last audience is kept when the provider cannot be read or the connection was removed. After that the audience is empty. Default 24 |

## Provider addresses

Each provider is reached at its public address unless you change it. Set these for GitHub Enterprise Server, a government or sovereign cloud, or a test double. Values must use `https://` when `ENVIRONMENT=production`; Checkcord will not start otherwise. `checkcord doctor` lists any that differ from the default. Jira takes its host per connection, from `checkcord connect jira`; it must use `https://`, and an `http://` host is accepted only when `ENVIRONMENT=development`.

| Variable | Default | Purpose |
|---|---|---|
| `GITHUB_API_URL` | `https://api.github.com` | GitHub REST API. For GitHub Enterprise Server, `https://HOST/api/v3` |
| `SLACK_API_URL` | `https://slack.com/api/` | Slack Web API. The install page is served from the same host at `/oauth/v2/authorize` |
| `MSGRAPH_URL` | `https://graph.microsoft.com` | Microsoft Graph, for Teams messages, directory, audiences and meeting transcripts. Token scopes are requested for this address |
| `MS_LOGIN_URL` | `https://login.microsoftonline.com` | Microsoft sign-in, for Graph tokens and the Teams admin consent page |
| `BOTFRAMEWORK_OPENID_URL` | `https://login.botframework.com/v1/.well-known/openidconfiguration` | Where the keys that sign Teams bot messages are published. The expected token issuer is read from it too |
| `LINEAR_API_URL` | `https://api.linear.app` | Linear GraphQL API and token exchange |
| `LINEAR_OAUTH_URL` | `https://linear.app` | Where the Linear consent page is served |
| `NOTION_API_URL` | `https://api.notion.com` | Notion API and consent page |

## Web console

| Variable | Default | Purpose |
|---|---|---|
| `WEB_BASE_URL` | `http://localhost:3000` | Where the console is served. Used in links, as the default allowed origin and as the default page after sign-in |
| `CORS_ALLOWED_ORIGINS` | `WEB_BASE_URL` | Origins allowed to call the API from a browser, comma-separated. Sign-in returns only to these and `WEB_BASE_URL` |
| `CHECKCORD_API_URL` | `http://localhost:CHECKCORD_API_PORT` | The API address the console calls from the browser. Set on the `web` container and read when it starts |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | For `npm run dev` only: the API address when `CHECKCORD_API_URL` is unset |
| `API_DOCS_ENABLED` | unset | Serve `/docs` and `/openapi.json`. Unset, they are served except when `ENVIRONMENT=production` |
| `CHECKCORD_API_PORT` | `8000` | Compose only. The host port the API is published on |
| `CHECKCORD_WEB_PORT` | `3000` | Compose only. The host port the console is published on |
| `CHECKCORD_BIND_ADDRESS` | `127.0.0.1` | Compose only. The host address the API and console are published on. Set `0.0.0.0` to reach them from other machines |
| `CHECKCORD_POSTGRES_PORT` | `5432` | Compose only. The host port of the bundled Postgres, on `127.0.0.1` |
| `CHECKCORD_REDIS_PORT` | `6379` | Compose only. The host port of the bundled Redis, on `127.0.0.1` |
| `CHECKCORD_IMAGE` | `checkcord:local` | Compose only. The image for the API, worker and migrations. Unset, it is built from the checkout. See [images](self-hosting.md#images) |
| `CHECKCORD_WEB_IMAGE` | `checkcord-web:local` | Compose only. The console image |

[self-hosting.md](self-hosting.md#addresses-and-ports) explains how these addresses and ports fit together.

## Proxies and rate limits

See [self-hosting.md](self-hosting.md#rate-limits).

| Variable | Default | Purpose |
|---|---|---|
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-For` header gives the client address, comma-separated. `*` trusts every sender; use it only when nothing else can reach the API |
| `RATE_LIMIT_ENABLED` | `true` | Turn every rate limit on or off |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | `600` | Requests per API key or sign-in session, on the API and `/mcp` |
| `RATE_LIMIT_AUTH_FAILURES_PER_MINUTE` | `30` | Rejected keys, sessions or tokens per client address. Past it the address is refused for the rest of the minute |
| `RATE_LIMIT_SIGN_IN_PER_MINUTE` | `30` | Requests to the sign-in endpoints per client address |
| `RATE_LIMIT_WEBHOOKS_PER_MINUTE` | `1200` | Webhook deliveries per connection |

## Health and metrics

See [self-hosting.md](self-hosting.md#monitoring).

| Variable | Default | Purpose |
|---|---|---|
| `METRICS_ENABLED` | `false` | Serve Prometheus metrics at `/metrics` on the API and the worker |
| `METRICS_TOKEN` | none | When set, `/metrics` needs `Authorization: Bearer <token>`. Set it whenever metrics are on |
| `WORKER_HTTP_HOST` | `127.0.0.1` | Where the worker serves `/healthz`, `/readyz` and `/metrics`. Compose sets `0.0.0.0` |
| `WORKER_HTTP_PORT` | `9100` | The worker's health and metrics port. `0` turns it off |

## Browser sign-in

Optional. Leave `OIDC_ISSUER` unset to use API keys only. See [sign-in.md](sign-in.md) for each provider.

| Variable | Default | Purpose |
|---|---|---|
| `OIDC_ISSUER` | none | The identity provider's issuer URL. Setting it and `OIDC_CLIENT_ID` turns sign-in on. Must be `https` in production |
| `OIDC_CLIENT_ID` | none | The client id at the provider |
| `OIDC_CLIENT_SECRET` | none | The client secret. Unset for a public client; PKCE is used either way |
| `OIDC_REDIRECT_URI` | `PUBLIC_BASE_URL` + `/api/v1/auth/callback` | The callback registered at the provider |
| `OIDC_SCOPES` | `openid email profile` | Scopes asked for, space- or comma-separated |
| `OIDC_ALLOW_MISSING_EMAIL_VERIFIED` | `false` | Accept an email claim that has no `email_verified`. An explicit `false` is always refused |
| `OIDC_AUTO_CREATE_DOMAINS` | none | Email domains, comma-separated, whose unknown people are created at their first sign-in. Unset, only existing people can sign in |
| `OIDC_ADMIN_GROUPS_CLAIM` | none | The ID token claim that lists a person's groups or roles. When set, the admin flag follows it at every sign-in |
| `OIDC_ADMIN_GROUPS` | none | Values of that claim that make a person an admin, comma-separated |
| `SCIM_ADMIN_GROUPS` | none | SCIM group names whose members are admins, comma-separated. When set, it alone decides the admin flag: the sign-in claim grants nothing and each sign-in sets the flag from SCIM membership. See [provisioning.md](provisioning.md#groups-and-admin-rights) |
| `SESSION_IDLE_MINUTES` | `60` | A session ends after this long without a request |
| `SESSION_MAX_HOURS` | `12` | A session ends this long after sign-in in any case |

## Retention

See [self-hosting.md](self-hosting.md#retention-and-deletion).

| Variable | Default | Purpose |
|---|---|---|
| `EVENT_RETENTION_DAYS` | unset | Days to keep events, counted from when they happened. A daily job at 03:00 UTC deletes older events and the records made only from them, and drops the empty weekly partitions they leave. Unset keeps events forever |
| `AUDIT_RETENTION_DAYS` | unset | Days to keep audit entries. Nothing deletes them on a schedule; `checkcord audit prune` deletes older entries. Unset keeps them forever |

## Review

See [self-hosting.md](self-hosting.md#reviewing-a-new-source).

| Variable | Default | Purpose |
|---|---|---|
| `REVIEW_NEW_CONNECTIONS` | `false` | Start every connection Checkcord has not seen before in review. Its events and records are held until an admin releases them. Connections that existed before are not changed |

## Tuning

The defaults suit most deployments.

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`, for `checkcord api` and `checkcord worker`. Other `checkcord` commands print only warnings and errors to stderr, unless run with `--verbose` or with `LOG_LEVEL=debug` or `info` set in their own environment. Audit entries are written to the database either way |
| `AUDIT_LOG_LINES` | `true` | Also write each audit entry as a log line on the `checkcord.audit` logger. The database entry is written either way |
| `WORKER_SWEEP_INTERVAL_SECONDS` | `60` | How often the worker retries unfinished events and trims stream entries that every consumer group has acknowledged |
| `REDIS_STREAM_BATCH_SIZE` | `10` | Messages a worker reads at a time |
| `REDIS_STREAM_POLL_TIMEOUT_MS` | `1000` | How long a worker waits for new messages |
| `REDIS_STREAM_RECLAIM_INTERVAL_MS` | `30000` | How often workers and the pull-request checks look for messages abandoned by a stopped process |
| `REDIS_STREAM_RECLAIM_MIN_IDLE_MS` | `120000` | How long a message must sit unacknowledged before it is reclaimed |
| `CHAT_BUFFER_MAX_MESSAGES` | `5` | Top-level chat messages buffered per channel before they are assigned to discussions |
| `CHAT_BUFFER_TIMEOUT_SECONDS` | `180` | Longest a channel buffer waits before it is assigned anyway |
| `CHAT_DISCUSSION_IDLE_SECONDS` | `1800` | How long a discussion must be quiet before what it settled is extracted |
| `CONFIDENCE_AGGREGATOR_INTERVAL_MINUTES` | `60` | How often confidence scores are recomputed |
| `JUDGE_BATCH_SWEEP_INTERVAL_MINUTES` | `30` | How often pending meeting extractions are audited when `MEETING_JUDGE_MODE=batch` |
| `REPO_BACKFILL_BATCH_SIZE` | `200` | Files parsed per batch when a repository is first connected |
| `AUDIENCE_REFRESH_BATCH_SIZE` | `500` | Events changed per transaction when an audience refresh touches a large channel or repository |
| `CI_LOG_EXCERPT_MAX_LINES` | `500` | Lines of a CI job log kept when no annotations are available |
| `SEARCH_MIN_SIMILARITY` | `0.3` | Lowest cosine similarity a `search_conceptual_context` result may have; a result that matches the query's words is kept below it. The scale depends on the embedding model: `0.3` suits OpenAI's `text-embedding-3` models, while local models such as `mxbai-embed-large` score unrelated text between about 0.25 and 0.5, so start near `0.5` for them. To tune it, search for something your records do not cover and raise it until that search returns little |
| `PERSONALIZATION_ENABLED` | `false` | Rank search results from repositories the caller has asked about ahead of organization-wide ones |

## Developer machines

These are read by `checkcord mcp` and `checkcord proxy` on a developer's machine, not by the server. See [agent-setup.md](agent-setup.md).

| Variable | Default | Purpose |
|---|---|---|
| `CHECKCORD_PROXY_API_URL` | none | Your Checkcord server. `checkcord mcp` forwards every tool call to its `/mcp` and needs this and `CHECKCORD_PROXY_API_KEY` |
| `CHECKCORD_PROXY_API_KEY` | none | The developer's own key |
| `CHECKCORD_PROXY_PORT`, `CHECKCORD_PROXY_HOST` | `8080`, `127.0.0.1` | Where the prompt proxy listens |
| `CHECKCORD_PROXY_USE_TLS` | `true` | Serve the prompt proxy over HTTPS with a local certificate |
| `CHECKCORD_PROXY_CERT_DIR` | `.context/certs` | Where that certificate is kept |
| `CHECKCORD_PROXY_OPENAI_UPSTREAM`, `CHECKCORD_PROXY_ANTHROPIC_UPSTREAM` | the providers' public APIs | Where the prompt proxy forwards requests |
| `CHECKCORD_PROXY_PROXY_MODE` | `base_url` | `base_url` expects the agent to be pointed at the proxy. `mitm` expects system-wide interception with a trusted local certificate authority |
| `CHECKCORD_PROXY_BYPASS` | `false` | Start the prompt proxy passing requests through untouched |
| `CHECKCORD_PROXY_WEB_BASE_URL` | `http://localhost:3000` | Viewer address used in links the proxy adds |

The prompt proxy's optional branch-collision warning reads `CHECKCORD_PROXY_DATABASE_URL` and `CHECKCORD_PROXY_TENANT_ID`. See [agent-setup.md](agent-setup.md#the-prompt-proxy-experimental).

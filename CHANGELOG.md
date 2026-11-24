# Changelog

All notable changes are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). Until 1.0, a minor version may include breaking changes; they are called out below.

## [0.1.0] - Unreleased

First public release. An early preview: everything below is covered by unit and integration tests, but no connector, identity provider or SCIM client has been verified against the live product yet. See the README's project status.

The database schema starts from a single baseline migration.

### Running it

- Docker Compose runs the API, the worker, migrations, the console, Postgres with pgvector and Redis; releases publish amd64 and arm64 images. See [self-hosting](docs/self-hosting.md#install) and [images](docs/self-hosting.md#images).
- A quick start on local models through Ollama, with sample data for an invented company (`checkcord demo load`). See the [README](README.md#quick-start) and [local models](docs/self-hosting.md#local-models).
- Any OpenAI-compatible chat model and Voyage or OpenAI-compatible embeddings; `checkcord doctor` checks the models, the database, the queue, keys and connections. See [configuration](docs/configuration.md#models) and [the `checkcord` command](docs/self-hosting.md#the-checkcord-command).

### Connectors

- Slack, Microsoft Teams (chat and meetings), GitHub (code and CI), Jira, Notion, Linear, Sentry, Datadog and Zoom, a signed webhook and a push API, each labelled beta or experimental. See [connector status](docs/connectors.md#connector-status).
- Per-connection scope, and review of new sources before anything they bring is served. See [choosing what is ingested](docs/connectors.md#choosing-what-is-ingested) and [reviewing a new source](docs/self-hosting.md#reviewing-a-new-source).
- Audiences read from Slack, Teams and GitHub and kept current, set by an admin for the other connectors, with accounts matched to people by email. See [who can read what](docs/connectors.md#who-can-read-what).

### Pipeline

- Filters noise, groups chat into discussions, extracts the decisions, constraints, requirements and risks they settled, and merges duplicates across sources into one record with an evidence score. See [ingestion pipeline](docs/architecture.md#ingestion-pipeline).
- Links records to classes and functions in Python, TypeScript, JavaScript, Go and Java. See [code graph](docs/architecture.md#code-graph).
- Events are stored before any model call, so a crash or a model outage loses nothing; records can be retired, and replacements are suggested for an admin to accept. See [store first, embed after](docs/architecture.md#store-first-embed-after) and [retiring a record](docs/self-hosting.md#retiring-a-record).

### Serving agents and reviews

- An MCP endpoint with three tools (decisions in scope, search, symbols in a file), and an experimental prompt proxy. See [agents](docs/agent-setup.md#what-the-agent-can-ask).
- A pull-request check and comment, and a `.context/ARCHITECTURE.md` file proposed to each repository through a pull request. See [pull-request guardrail](docs/architecture.md#pull-request-guardrail).

### Access control

- Row-level security per organization and per person; personal data is redacted when stored and again on every read, and `checkcord redaction recompute` redoes stored copies when the redaction rules or the model change. See [what is enforced](docs/security.md#what-is-enforced).
- Browser sign-in with OpenID Connect, and SCIM 2.0 users and groups. See [sign-in](docs/sign-in.md) and [provisioning](docs/provisioning.md).
- An audit log of every read of record content and every admin action, purges, retention, and removed items that stay removed. See [audit log](docs/self-hosting.md#audit-log) and [retention and deletion](docs/self-hosting.md#retention-and-deletion).

### Administration

- The `checkcord` command, an admin HTTP API and a web console, with model usage and cost per organization. See [admin API](docs/self-hosting.md#admin-api), [console](docs/self-hosting.md#console) and [usage and cost](docs/self-hosting.md#usage-and-cost).

### Operations

- Rate limits, Prometheus metrics, health checks, and several replicas of the API and worker. See [monitoring](docs/self-hosting.md#monitoring) and [scaling](docs/self-hosting.md#scaling).

### Extending

- A connector registry that other packages can add to, and example scripts in `examples/`. See [extending](docs/extending.md).

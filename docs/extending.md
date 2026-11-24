# Extending Checkcord

This page is for engineers who want to send Checkcord data from a system it does not support, or build on its API. It names the extension points, what each one guarantees, and the files to read.

| You want to | Use | Code to write |
|---|---|---|
| Send events from a script or a job | [The push API](#the-push-api) | A few lines, any language |
| Send events from a system that can sign a webhook | [The signed webhook connector](#the-signed-webhook-connector) | None on the Checkcord side |
| Add a source with its own webhook, poller or setup | [A connector](#writing-a-connector), built in or in its own package | A Python module |
| Read records from an agent or a service | [MCP](#mcp-tools) or [the HTTP API](#the-http-api) | A client |
| Add an MCP tool | [MCP tools](#adding-an-mcp-tool) | A service function and its tests |

The scripts in [`examples/`](../examples) use the Python standard library only:

- `push_events.py` pushes two events, one public and one for named people.
- `signed_webhook.py` signs and sends one event to a signed webhook connection.
- `mcp_read.py` asks `/mcp` for records as the person who owns the key.

## The push API

`POST /api/v1/ingest/custom` takes one event and any valid API key of the organization. The fields are in [`CustomPushPayload`](../src/checkcord/schemas/push.py); only `content_raw` is required. `access_control` sets the audience:

- `{"is_public": true, "visibility": "public", "acl_type": "open"}`: everyone in the organization.
- `{"is_public": false, "visibility": "private", "acl_type": "custom", "allowed_identity_ids": [...]}`: the people with those user ids (from `checkcord users list` or `GET /api/v1/admin/users`).
- Left out: private with nobody named. The event is stored, but no person-scoped read returns it.

Everything pushed shares one connection, `custom:push`, so it has one review state and no scope. The sender chooses the audience, and Checkcord does not check it against anything. [connectors.md](connectors.md#push-api) has a `curl` example.

## The signed webhook connector

For a system that can send JSON and sign it with a shared secret, use the signed webhook connector instead of writing code. Each connection has its own secret, review state and scope, and the audience is given as email addresses that Checkcord matches to its people.

```bash
checkcord connect webhook --name status-page
```

[connectors.md](connectors.md#signed-webhook) describes the request. The [walkthrough](#walkthrough-the-signed-webhook-connector) below reads its code.

## Writing a connector

A connector is one [`Connector`](../src/checkcord/connectors/base.py) value. Checkcord reads every connector from one registry, [`checkcord.connectors.registry`](../src/checkcord/connectors/registry.py), and these places list it from there:

- `checkcord connect NAME`, with its status in `checkcord connect --help`;
- `checkcord connections`, `checkcord disconnect`, and `GET /api/v1/admin/connections` with its `status`;
- `checkcord connections scope`, when the connector has a `scope`;
- `checkcord audiences`, when it has an `audience_platform`;
- `checkcord secrets encrypt` and the `checkcord doctor` secrets check, for its `secret_columns`;
- the `checkcord doctor` `connectors` line, which also reports any package that failed to load;
- the API routes, from its `routers`, the scheduler, from its `poll`, and a long-running task in the API or the worker, from its `listener`;
- install links in the console and `POST /api/v1/admin/connections/install-link`, when it has an `oauth` app.

### The definition

| Field | Meaning |
|---|---|
| `name` | The provider part of the connection key `name:identifier`. Unique, no `:` |
| `title`, `ingestion` | Shown to operators, for example `Signed webhook` and `webhook` |
| `status` | `beta` or `experimental`; see [connector status](connectors.md#connector-status) |
| `scope` | What `checkcord connections scope` lists for it, such as `Jira project keys`. Leave it out if the connector cannot honour a scope |
| `audience_platform` | Set only when the connector stores per-container audiences that `checkcord audiences refresh` can read again. Refreshing needs a reader in [`services/audiences.py`](../src/checkcord/services/audiences.py), which a separate package cannot add yet |
| `table`, `identifier` | The model that stores its connections and the column that is the identifier. Use `ConnectorInstallation` and `"identifier"` unless the connector needs columns of its own |
| `secret_columns` | Columns on `table` that hold secrets encrypted with `core.crypto.encrypt_secret` |
| `sealed_json` | `(column, seal)` pairs for JSON columns whose token fields `checkcord secrets encrypt` seals with `seal` |
| `routers` | `(APIRouter, tag)` pairs for its webhooks, OAuth redirect or admin routes. A `None` tag leaves the router's own tags |
| `poll` | `Poll(job_id, run, minutes, publishes=True)`: the scheduler job id, an async `run(engine, bus) -> int` and the interval, read from settings. Each run happens in one process per interval. A job that publishes is scheduled only when Redis is configured; one with `publishes=False` always runs and gets `None` for the bus |
| `listener` | `Listener(run, process)`: an async `run(ListenerContext)` that lasts as long as the `"api"` or `"worker"` process. It starts once the database and the event bus are open and is cancelled before they close |
| `setup` | `Setup(help, run, arguments)` for `checkcord connect NAME`. The command adds `--tenant`, `--public-url` and `--review`. `run` gets a `SetupContext` and prints what to configure on the other side. A `ValueError` is shown to the operator |
| `oauth` | `OAuthApp(client_id_field, redirect_field, notes)`: the settings an install link needs, and lines `checkcord connect NAME` prints after the link |
| `disconnect` | An async `disconnect(row, settings)` that cleans up at the provider before `checkcord disconnect` deletes the row, such as revoking a token |

Every built-in connector is defined this way, in a `connector.py` next to its code. Worth reading next to the sample:

- Datadog, a webhook: [`adapters/monitoring/providers/datadog/connector.py`](../src/checkcord/adapters/monitoring/providers/datadog/connector.py).
- Linear, an OAuth app and a poller: [`adapters/tasks/providers/linear/connector.py`](../src/checkcord/adapters/tasks/providers/linear/connector.py).
- GitHub, several routers, a poller and a setup that queues a backfill: [`adapters/code/providers/github/connector.py`](../src/checkcord/adapters/code/providers/github/connector.py).
- Slack, a Socket Mode listener and token revocation on disconnect: [`adapters/chat/providers/slack/connector.py`](../src/checkcord/adapters/chat/providers/slack/connector.py).
- Teams meetings, a job that does not publish and a Graph subscription removed on disconnect: [`adapters/meetings/providers/msteams/connector.py`](../src/checkcord/adapters/meetings/providers/msteams/connector.py).

`main.py`, `worker.py`, `pipeline/scheduler.py` and `cli/commands.py` read every connector from the registry and name none of them.

### Registering it

A connector in this repository goes in a module that exposes `CONNECTORS`, listed in `_BUILTIN_MODULES` in [`registry.py`](../src/checkcord/connectors/registry.py).

A connector in its own package declares an entry point in the `checkcord.connectors` group that names a `Connector` or a list of them:

```toml
[project.entry-points."checkcord.connectors"]
status-page = "checkcord_status_page:CONNECTORS"
```

Install the package next to Checkcord, for example in an image built `FROM` the Checkcord image, and restart the API and the worker. `checkcord doctor` lists it on its `connectors` line. A package that fails to import, names something other than a `Connector`, or reuses a built-in name is skipped, and the `connectors` line fails with the reason. [`tests/fixtures/connector_plugin`](../tests/fixtures/connector_plugin) is a minimal package, and [`tests/connectors/test_connector_registry.py`](../tests/connectors/test_connector_registry.py) installs it.

A package can use `ConnectorInstallation` and `checkcord.connectors.installations` for storage, so it needs no migration. A package with its own table needs its own migration, which Checkcord's `checkcord migrate` does not run.

### What every connector must do

These are what keep Checkcord's guarantees; review checks them.

1. **Verify the sender first.** Check the signature or token with `hmac.compare_digest` before parsing anything, and answer `401` the same way for a bad signature and an unknown connection.
2. **Rate limit** with `await rate_limit.webhook(request, connection_key)` after verifying.
3. **Honour the scope** with `scope.allows(engine, tenant_id, connection_key, container)` before storing anything or calling a model. If the source has nothing to scope by, leave `scope` unset.
4. **Set the audience from the source.** Public only when the source says everyone in the organization can read it. Otherwise list the people Checkcord can match, and leave the list empty rather than guess.
5. **Set `connection`** on every `CanonicalEvent` to the connection key, so review, purge and `checkcord disconnect --purge` find it.
6. **Store secrets encrypted**, and name their columns in `secret_columns`. Show a generated secret once.
7. **Publish** to an `events.raw.*` stream. A connector without its own worker uses `events.raw.custom`.
8. **Test** with synthetic payloads: unit tests for parsing and signatures, and integration tests (marker `integration`) for storage, scope, audience and tenant isolation. Do not commit real data.

Review needs no code: `installations.save(..., review=True)` puts a new connection in review, and the pipeline holds what it brings.

## Walkthrough: the signed webhook connector

The sample connector is [`src/checkcord/connectors/signed_webhook/`](../src/checkcord/connectors/signed_webhook). It is the smallest complete connector, and the template to copy.

### `__init__.py`: the definition and setup

`CONNECTORS` holds one `Connector` named `webhook`. It stores connections in `ConnectorInstallation`, keyed by `identifier` (the `--name` given to `checkcord connect webhook`), with the signing secret in the encrypted `secret` column. `scope` is set because each event names a `container`. `audience_platform` is not set: the audience comes with each event, so there is nothing to refresh.

`_arguments` adds `--name` and `--rotate-secret`. `_setup` generates a secret, saves the connection with `installations.save`, which also audits it and applies `--review`, and prints the URL, the headers and a sample body. The secret is printed only when it is new.

### `routes.py`: the webhook

`POST /api/v1/webhooks/signed/{installation_id}` does, in order:

1. Loads the connection with `installations.by_id`, which looks it up across organizations and returns its tenant.
2. Checks that `X-Checkcord-Timestamp` is within 300 seconds and that `X-Checkcord-Signature` is `sha256=` and the HMAC-SHA256 of `<timestamp>.<body>` under the secret. Any failure, including an unknown id, is a `401` with the same body.
3. Applies the per-connection webhook rate limit.
4. Validates the body (`422` on error).
5. Asks `scope.allows` about the event's `container`; a refused event answers `202` with `"status": "skipped"` and is counted.
6. Resolves `audience.emails` to active people with `people.by_email`, ignoring case, and reports how many matched nobody in `unknown_emails`.
7. Publishes the event to `events.raw.custom` and answers `202` with its `event_id`.

### `payload.py`: the event

`SignedWebhookEvent` is the request body. `to_event` turns it into a `CanonicalEvent`: source platform `custom`, the connection key, the container as `source_group_id`, and the audience. An event is public only when `audience.public` is true; otherwise it is private to the people resolved in step 6 and any `audience.identity_ids`. The text is kept out of `source_raw_payload` so it is stored once.

### Storage

[`ConnectorInstallation`](../src/checkcord/storage/models.py) and its migration create `connector_installations`, with row-level security on `tenant_id` like every tenant table. [`connectors/installations.py`](../src/checkcord/connectors/installations.py) saves, finds and lists rows and decrypts secrets. `checkcord connections` and `checkcord disconnect` filter the table by provider, so many connectors can share it.

### Tests

- [`tests/connectors/test_signed_webhook.py`](../tests/connectors/test_signed_webhook.py): the signature, the time window, the audience and required fields.
- [`tests/connectors/test_signed_webhook_db.py`](../tests/connectors/test_signed_webhook_db.py) (integration, as the app database role): an accepted event published with its readers, refused signatures, scope, rotation and disconnect, and that another organization cannot see the connection.

## The HTTP API

The API describes itself with OpenAPI at `/openapi.json`, and `/docs` renders it. Both are off when `ENVIRONMENT=production`, unless `API_DOCS_ENABLED=true`. A copy for this version is in [`openapi.json`](openapi.json). `tests/test_openapi_doc.py` fails when the copy is out of date; refresh it with:

```bash
uv run python scripts/export_openapi.py
```

### Authentication

- **Integrations** send `Authorization: Bearer <API key>`. Create a key with `checkcord keys create --name NAME`. Add `--admin` for `/api/v1/admin/` and SCIM, or `--user EMAIL` so reads are filtered to what that person may see. A key without a person reads public records only.
- **Browsers** sign in through `/api/v1/auth/login` and send the session cookie, with the `X-CSRF-Token` header from `GET /api/v1/auth/me` on requests that change state. Ingestion endpoints do not take sessions.
- **Webhooks** carry the sending system's own signature or token, as each connector describes.

### Stable routes

Until 1.0, a change to these routes is listed as **Breaking** in the [changelog](../CHANGELOG.md):

- `/mcp`, its three tools and their arguments;
- `POST /api/v1/ingest/custom`;
- `POST /api/v1/webhooks/signed/{installation_id}`;
- everything under `/api/v1/admin/`, with lists shaped `{"items": [...], "next_cursor": ...}` and errors as `{"detail": ...}`;
- SCIM 2.0 under `/scim/v2/`;
- `/api/v1/auth/*`, `/healthz` and `/readyz`.

The `/api/v1/graph/*` and `/api/v1/agents/*` routes serve the console and the developer proxy, and may change without notice. Connector webhook routes belong to the system that calls them.

## MCP tools

`/mcp` speaks MCP over streamable HTTP, statelessly, so each request can be a single JSON-RPC call. [`examples/mcp_read.py`](../examples/mcp_read.py) shows one, and [agent-setup.md](agent-setup.md) shows agent configuration.

### Adding an MCP tool

Every tool has the same three properties, and a new one must keep them:

- **Per-person filtering.** Run every query inside `tenant_context(engine, tenant_id, user_id=...)`, with the user id from the caller's key. Row-level security then returns only what that person may read. Never use `app_bypass_rls_context` for content.
- **Redaction.** Pass record rows through `redaction.serve_field` (or text through `redaction.serve`) before returning them.
- **Audit.** Call `audit.record(...)` on the same connection, inside the same transaction, with the action `mcp.<tool name>`, the parameters, the record ids returned and the redacted count. If the entry cannot be written, the read fails. Add the action to `audit.AGENT_READS` in [`storage/audit.py`](../src/checkcord/storage/audit.py) so `checkcord stats` counts it.

The steps:

1. Write the tool as a function in [`services/mcp_tools.py`](../src/checkcord/services/mcp_tools.py) that takes the engine, the tenant id and the `audit.Actor`, like `active_decisions`.
2. Register it in `build_mcp_server` in [`api/mcp_http.py`](../src/checkcord/api/mcp_http.py), and add the forwarding tool to [`proxy/mcp_server.py`](../src/checkcord/proxy/mcp_server.py) so `checkcord mcp` offers it too.
3. Test it: unit tests in [`tests/services/test_mcp_tools.py`](../tests/services/test_mcp_tools.py) for redaction and the audit entry, and integration tests in [`tests/api/test_mcp_http.py`](../tests/api/test_mcp_http.py) showing that a person's key, an unbound key, an admin key and another organization's key each see only what they should.

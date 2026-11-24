# Self-hosting

This page takes you from an empty machine to a running deployment, then covers what to change for production, how to upgrade, and what to back up.

## What you need

- A Linux host or VM with Docker and Docker Compose v2. 2 vCPU and 4 GB of memory is enough to start.
- An API key for an OpenAI-compatible chat model. Checkcord uses it to filter, extract and merge context.
- An embeddings key: Voyage (default) or OpenAI.
- Or, instead of both keys, a model server on your own hardware. See [Local models](#local-models).
- A hostname with TLS in front of the API if any connected system has to reach it with webhooks. Slack in Socket Mode and the push API work without inbound access.

## Install

```bash
git clone <this repository> checkcord && cd checkcord
cp .env.example .env
```

Fill in `.env`:

| Setting | How to get it |
|---|---|
| `FERNET_MASTER_KEY` | `docker run --rm python:3.12-slim sh -c "pip -q install cryptography && python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"` |
| `POSTGRES_PASSWORD`, `CHECKCORD_OWNER_DB_PASSWORD`, `CHECKCORD_APP_DB_PASSWORD` | `openssl rand -hex 24`, once for each |
| `OPENAI_API_KEY` | Your model provider |
| `VOYAGE_API_KEY` | Voyage, or set `EMBEDDING_PROVIDER=openai` and leave this empty |
| `PUBLIC_BASE_URL` | The URL connected systems will call, for example `https://checkcord.example.com` |

Keep `FERNET_MASTER_KEY` somewhere safe. It encrypts the credentials of every connected system. If you lose it you have to reconnect them all.

Start everything:

```bash
docker compose up -d --build --wait
docker compose exec api checkcord doctor
docker compose exec api checkcord init
```

`checkcord init` prints the first admin key once. Store it in your secret manager. `checkcord doctor` should show every line as `PASS` after that.

What is now running:

| Service | Port | Purpose |
|---|---|---|
| `api` | `CHECKCORD_API_PORT`, default 8000 | HTTP API, webhooks, scheduled jobs |
| `worker` | 9100, on the Compose network only | Processes queued events. Serves health and metrics |
| `web` | `CHECKCORD_WEB_PORT`, default 3000 | Admin console and context graph viewer |
| `postgres` | 127.0.0.1, `CHECKCORD_POSTGRES_PORT`, default 5432 | All stored data |
| `redis` | 127.0.0.1, `CHECKCORD_REDIS_PORT`, default 6379 | Event queue and rate-limit counters |

If another program already uses port 8000 or 3000, set `CHECKCORD_API_PORT` or `CHECKCORD_WEB_PORT` in `.env`. See [Addresses and ports](#addresses-and-ports).

## Images

Compose runs the images named by `CHECKCORD_IMAGE` (API, worker and migrations) and `CHECKCORD_WEB_IMAGE` (console). Unset, they are `checkcord:local` and `checkcord-web:local`, built from this checkout. Either way works:

- **Build here**, as above: `docker compose up -d --build --wait`.
- **Run a release.** Each release publishes images for `linux/amd64` and `linux/arm64` to the GitHub Container Registry of the repository it was released from, tagged with the version and `latest`. Set both in `.env`, then pull and start:

  ```bash
  CHECKCORD_IMAGE=ghcr.io/mihirp381/checkcord:0.1.0
  CHECKCORD_WEB_IMAGE=ghcr.io/mihirp381/checkcord/web:0.1.0
  ```

  ```bash
  docker compose pull
  docker compose up -d --wait
  ```

  Without `--build`, Compose builds only an image it cannot find. Keep the checkout at the same version as the images: Compose reads `docker-compose.yml` and `deploy/postgres/` from it.

The images carry build provenance and an SBOM. Inspect them with `docker buildx imagetools inspect ghcr.io/mihirp381/checkcord:0.1.0`.

## Console

The console is at `WEB_BASE_URL`, `http://localhost:3000` by default. Admins manage connections, review, replacements, records, people, keys, the audit log, audiences and the pipeline there. Everyone else sees the context graph and their own account. The API enforces admin rights; the console only hides what a person cannot use.

To sign in the first time, choose **Continue with an API key** and paste the key from `checkcord init`. The key stays in that browser tab until you sign out or close the tab.

With browser sign-in configured ([sign-in.md](sign-in.md)), the page shows a **Sign in** button. Create each person first, or set `OIDC_AUTO_CREATE_DOMAINS`, and give admins the flag with `checkcord users admin EMAIL on`. After the provider, the browser comes back to the page it asked for. That page must be on `WEB_BASE_URL` or `CORS_ALLOWED_ORIGINS`. If the provider or Checkcord refuses the sign-in, the API answers with the reason instead of going back.

Without sign-in configured, the page says so and takes an API key only. Give each person a key bound to them (`checkcord keys create --user EMAIL`); they see what that key may see.

New connections are still made with `checkcord connect`. The console shows the command, and lists, pauses for review and disconnects what is connected.

The console calls the API at `CHECKCORD_API_URL`, read when the `web` container starts. Change it and run `docker compose up -d web`; no rebuild is needed.

## Addresses and ports

Four settings hold addresses. They must agree with each other and with your proxy.

| Setting | Who uses it | Typical value |
|---|---|---|
| `PUBLIC_BASE_URL` | Connected systems. Checkcord prints it in webhook and install instructions, and builds the sign-in callback and the Teams meetings subscription from it | `https://checkcord.example.com` |
| `WEB_BASE_URL` | People. The console is served here. Used in links, as the default allowed origin, and as the default page after sign-in | `https://console.example.com` |
| `CHECKCORD_API_URL` | The console. Browsers call the API at this address. Read when the `web` container starts | The same as `PUBLIC_BASE_URL` |
| `CORS_ALLOWED_ORIGINS` | The API. Browser origins allowed to call it. Defaults to `WEB_BASE_URL` | Leave unset unless the console is served at a second address |

`CHECKCORD_API_PORT` and `CHECKCORD_WEB_PORT` set the host ports Compose publishes. They default to 8000 and 3000. Inside the containers the ports stay 8000 and 3000. Compose publishes them on `127.0.0.1` only; set `CHECKCORD_BIND_ADDRESS=0.0.0.0` to reach them from other machines. The ports do not change the four addresses:

- Without a proxy, set `PUBLIC_BASE_URL` and `CHECKCORD_API_URL` to `http://HOST:CHECKCORD_API_PORT`, and `WEB_BASE_URL` to `http://HOST:CHECKCORD_WEB_PORT`. When `CHECKCORD_API_URL` is unset, Compose uses `http://localhost:CHECKCORD_API_PORT`.
- With a proxy, the proxy forwards to the host ports, and the four addresses are the proxy's public addresses. Serve the console and the API on the same site so the browser sends the sign-in cookie.

`FORWARDED_ALLOW_IPS` lists the proxies the API trusts. The API reads the client address from `X-Forwarded-For` only on requests from these addresses. The default is `127.0.0.1`. Per-address rate limits use the client address. With Compose, a proxy on the host reaches the API from the Docker network gateway, not from `127.0.0.1`. Set `FORWARDED_ALLOW_IPS` to that gateway (`docker network inspect checkcord_default`; the Compose project is named `checkcord`) or to the proxy's own address. Otherwise every client looks like the proxy and they share one per-address limit.

## The `checkcord` command

Run it inside the API container: `docker compose exec api checkcord <command>`.

Output goes to stdout. Warnings and errors go to stderr; add `--verbose` to see the info log lines too, audit entries included. Every command is audited in the database whether or not its log lines are shown.

| Command | What it does |
|---|---|
| `checkcord doctor` | Checks the database, the database role and its audit grants, migrations, the embedding column width, Redis, keys, encryption, that the chat endpoint lists every configured model, that the embedding model answers with `EMBEDDING_DIMENSION`-wide vectors, the Teams meetings subscription, that `MEETING_JUDGE_MODE=batch` is used only with OpenAI, and sign-in: whether it is configured and whether the issuer answers. Warns about failed events, dead-letter entries, channel or repository audiences not read within `AUDIENCE_REFRESH_GRACE_HOURS`, and connections in review with their held record count |
| `checkcord events failed` | Lists events whose embedding step gave up, and chat discussions parked after five unusable extraction replies (channel, message count, attempts, last error) |
| `checkcord events retry --all` or `--id UUID` | Puts failed events and parked discussions back in the retry sweep |
| `checkcord dlq list` | Lists dead-letter streams with their entry counts |
| `checkcord dlq show STREAM` | Lists a stream's entries with their error and age. Message content is not shown |
| `checkcord dlq replay STREAM --all` or `--id ID` | Puts entries back on their original stream and removes them from the dead-letter stream |
| `checkcord dlq drop STREAM --all` or `--id ID` | Deletes entries for good |
| `checkcord init` | Creates the first admin key |
| `checkcord keys create`, `list`, `revoke` | Manages API keys |
| `checkcord users list` | Lists active people with their admin flag. `--query TEXT` filters by email or name, `--all` includes deactivated people |
| `checkcord users admin EMAIL on` or `off` | Gives or takes a person's admin rights for sign-in |
| `checkcord sessions list` | Lists live sign-in sessions. `--user EMAIL` filters, `--all` includes ended ones |
| `checkcord sessions revoke ID` | Ends a session at once |
| `checkcord connect <system>` | Connects a system and prints what to configure on its side. `--review` holds what it brings |
| `checkcord connections` | Lists what is connected, and whether each connection is live or in review |
| `checkcord connections review CONNECTION on` or `off` | Holds a connection's new material, or stops holding it. `CONNECTION` is `provider:identifier` as listed |
| `checkcord connections scope CONNECTION show`, `include`, `exclude` or `clear` | Shows or changes which channels, repositories, projects or pages a connection ingests. `--remove` takes names off a list. See [connectors.md](connectors.md#choosing-what-is-ingested) |
| `checkcord review list` | Lists held records, redacted, with their sources and event counts. Filter with `--connection` |
| `checkcord review release CONNECTION` or `--record UUID` | Makes held material readable. Releasing a connection also makes it live |
| `checkcord review reject CONNECTION` or `--record UUID` | Deletes held material and the events linked only to it. The connection stays in review |
| `checkcord records show UUID` | Prints a record, redacted, with its state, severity, scope, audience size, sources, event count and dates, up to 20 updates, and any suggested replacement |
| `checkcord records severity UUID blocking` or `advisory` | Sets whether a pull request that touches the record fails the check |
| `checkcord records type UUID adr`, or `constraint`, `business_requirement`, `risk`, `general` | Sets the record's type: the first four make it a decision record, `general` a reference record. Audited as `record.type_changed`; later events and score updates keep it |
| `checkcord records retire UUID` | Stops serving a record that no longer holds. `--replaced-by UUID` names the live record that replaces it, `--reason TEXT` says why |
| `checkcord records restore UUID` | Serves a retired record again |
| `checkcord records suggestions` | Lists suggested replacements: a newer record that may replace an older one |
| `checkcord records accept ID` or `dismiss ID` | Accepts a suggestion, which retires the older record with the newer one as its replacement, or dismisses it |
| `checkcord disconnect <system> <identifier>` | Removes a connection and its stored credential, then prints how much content is left. With `--purge`, deletes that content too |
| `checkcord purge source --platform P --container ID` | Deletes every event of one channel, repository or space and what was made from it |
| `checkcord purge event --id UUID` | Deletes events and what was made from them. Repeat `--id` for more |
| `checkcord purge record --id UUID` | Deletes a record and the events linked only to it |
| `checkcord users delete EMAIL` | Deletes a person, their keys, the events they wrote and their place in every audience |
| `checkcord removed count` | Prints how many removed items are remembered per platform. `checkcord removed clear` forgets them so a later delivery is stored again |
| `checkcord migrate` | Applies database migrations and sizes the embedding column to `EMBEDDING_DIMENSION`. `--reset-embeddings` clears stored vectors when the width must change |
| `checkcord demo load` | Sends sample data for an invented company through the pipeline, waits for it (`--wait SECONDS`, default 300), and prints two sample keys, an MCP `curl` command and an agent configuration |
| `checkcord demo remove` | Deletes the sample data: events, records, people, keys, code symbols, audiences and the review hold. Audit entries stay |
| `checkcord audiences list` | Lists the stored audience of each Slack channel, Teams conversation and GitHub repository: public or private, member count, when it was last read, and the last error. Members are not shown |
| `checkcord audiences refresh --platform P --container ID` or `--all` | Reads audiences from the provider now and applies them to their events and records. `P` is `slack`, `ms_teams` or `github`; `ID` is a channel id or `owner/name` |
| `checkcord audit list` | Shows audit entries, newest first. Filter with `--since`, `--until`, `--user`, `--key`, `--action`, `--channel` and `--record` |
| `checkcord audit export --since TIME --until TIME` | Writes audit entries in a time range as JSON Lines, or CSV with `--format csv` |
| `checkcord audit prune` | Deletes audit entries older than `AUDIT_RETENTION_DAYS`, or `--days N` |
| `checkcord redaction recompute` | Redacts again every record summary whose stored copy is on older rules or another model. Runs in batches of `--batch-size` (default 100). Stopped, it picks up where it left off |
| `checkcord usage` | Model calls, tokens and failures by purpose and model for the last 30 days, or `--days N`. Shows estimated cost when `MODEL_PRICES` is set |
| `checkcord stats` | Activity for the last 7 days, or `--days N`: chat messages kept and other events by source and arrival day, records (decisions by type, then reference records), records with removed instructions, unfinished events, chat messages waiting to be grouped into a discussion, waiting, failed and empty chat discussions, dead letters, agent reads, pull-request checks (posted, with conflicts, blocked, errors, and skipped because the branch is Checkcord's own) and connections |

Run any command with `--help` for its options. The `purge`, `review reject`, `users delete`, `disconnect --purge` and `audit prune` commands print counts and ask before they delete. `--dry-run` prints the counts only, and `--yes` skips the question.

## Production checklist

1. **Put TLS in front.** Run a reverse proxy (Caddy, nginx, a cloud load balancer) that terminates TLS and forwards to the API port (`CHECKCORD_API_PORT`, 8000), and to the console port (`CHECKCORD_WEB_PORT`, 3000). Do not expose the API port directly. Compose publishes it on `127.0.0.1` unless `CHECKCORD_BIND_ADDRESS` says otherwise. Set `FORWARDED_ALLOW_IPS` to the proxy's address. See [Addresses and ports](#addresses-and-ports).
2. **Keep `ENVIRONMENT=production`.** In this mode the API and worker refuse to start without an encryption key, with a default database password, or when their database role would bypass row-level security.
3. **Use two database roles.** Compose sets this up for you. `checkcord_owner` runs migrations. `checkcord_app` serves the API and worker and cannot bypass row-level security. On a managed Postgres, run [`deploy/postgres/roles.sql`](../deploy/postgres/roles.sql) as the admin user, set both passwords, and set `DATABASE_URL` and `MIGRATION_DATABASE_URL` yourself. The database needs the `vector` extension (pgvector).
4. **Restrict who reaches the console.** Configure browser sign-in ([sign-in.md](sign-in.md)) and serve the console only where your own staff can reach it. A key pasted into the console is held in the browser tab; see [security.md](security.md).
5. **Set `WEB_BASE_URL` and `CHECKCORD_API_URL`** to the public addresses of the console and the API. `CHECKCORD_API_URL` is read when the `web` container starts. Serve both on the same site so the browser sends the sign-in cookie.
6. **Keep the API docs off.** In production `/docs` and `/openapi.json` are not served. Set `API_DOCS_ENABLED=true` only if you want them public; they list routes, not data.
7. **Turn on monitoring.** Set `METRICS_ENABLED=true` and `METRICS_TOKEN`, scrape both processes and add the alerts in [Monitoring](#monitoring).
8. **Scale only what can scale.** You can run several `api` and `worker` processes; `migrate` runs once. See [Scaling](#scaling).

## Scaling

You can run more than one `api` and more than one `worker`. They must share one Postgres and one Redis. `migrate` runs once, before the others start.

With Compose, one published host port serves one container. To run two API containers, publish a port range in an override file and point your proxy at both ports:

```yaml
# docker-compose.override.yml
services:
  api:
    ports: !override
      - "127.0.0.1:8000-8001:8000"
    deploy:
      replicas: 2
  worker:
    deploy:
      replicas: 2
```

What keeps this safe:

- **Scheduled jobs.** Every API process runs the scheduler. Before a job runs, the process takes a Postgres advisory lock named after the job, then claims the run in the `scheduled_jobs` table. The others skip it and log `scheduled_job.skipped`. A job runs at most once per interval, less a small allowance for clock drift: a tenth of the interval, at most a minute. Daily and weekly jobs run at most once an hour. If the process running a job dies, Postgres drops its lock with its connection, and the next process to try runs the job.
- **Worker sweeps.** The redaction sweep uses the same lock. The event retry sweep claims rows with `FOR UPDATE SKIP LOCKED`. Trimming streams is safe to repeat.
- **Queued events.** Workers share each Redis consumer group, so each message goes to one worker. If a worker dies, another one reclaims its unacknowledged messages after `REDIS_STREAM_RECLAIM_MIN_IDLE_MS`.
- **Pull-request checks.** Each API process also runs the pull-request evaluator and status reporter. They share consumer groups, and a delayed status is handed to one process only. A message read by an API process that dies is reclaimed by another after `REDIS_STREAM_RECLAIM_MIN_IDLE_MS`.
- **Slack Socket Mode.** Each API process opens its own connection. Slack sends each event to one of them, up to 10 connections.
- **Rate limits** are counted in Redis, so they hold across processes. Model usage counts are kept by each process and added to the same rows.
- **The key cache** is per process. See [security.md](security.md#what-is-not-enforced).

Scale `api` for request load and `worker` for pipeline backlog (`checkcord_stream_lag`). The web console is stateless; run as many as you like.

## Rate limits

The API counts requests in Redis, in one-minute windows, and answers `429 Too Many Requests` with `Retry-After` when a limit is reached.

| Limit | Default per minute | Counted by |
|---|---|---|
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | 600 | API key or sign-in session, on the API and `/mcp` |
| `RATE_LIMIT_AUTH_FAILURES_PER_MINUTE` | 30 | Client address. Rejected keys, sessions and tokens. Once reached, the address gets 429 for the rest of the window, even with a good key |
| `RATE_LIMIT_SIGN_IN_PER_MINUTE` | 30 | Client address, on `/api/v1/auth/login` and `/api/v1/auth/callback` |
| `RATE_LIMIT_WEBHOOKS_PER_MINUTE` | 1200 | Connection, such as one GitHub repository or one Jira site. Counted after the signature check |

`/healthz` and `/readyz` are not limited. A wrong `METRICS_TOKEN` counts as a failed authentication, so scrape from an address your users do not share. The client address comes from `FORWARDED_ALLOW_IPS`; see [Addresses and ports](#addresses-and-ports).

When Redis cannot be reached:

- Requests with a valid key or session, and webhooks, are let through. A Redis outage does not stop the API.
- Failed authentication and sign-in attempts are still limited, by a count held in each API process. With several processes an address gets that many attempts at each.

Each case adds to `checkcord_rate_limit_redis_errors_total`. Set `RATE_LIMIT_ENABLED=false` to turn every limit off, for example when your proxy already limits.

## Monitoring

### Health

| Process | Liveness | Readiness |
|---|---|---|
| `api` | `GET /healthz` on 8000 | `GET /readyz` on 8000 |
| `worker` | `GET /healthz` on `WORKER_HTTP_PORT` (9100) | `GET /readyz` on 9100 |

Liveness answers 200 while the process runs. Readiness answers 200 when Postgres answers, Redis answers and the database is at the migration head of this version. Otherwise it answers 503 and names the part that failed, for example `{"status": "not ready", "checks": {"postgres": "ok", "redis": "unreachable: ..."}}`. The Compose healthchecks use readiness. Restart a process on failed liveness only; take it out of service on failed readiness.

The worker serves on `WORKER_HTTP_HOST`, `127.0.0.1` by default. Compose sets it to `0.0.0.0` so other containers on the Compose network can reach it; the port is not published on the host. Set `WORKER_HTTP_PORT=0` to turn it off.

### Metrics

Set `METRICS_ENABLED=true` to serve Prometheus metrics at `GET /metrics` on the API and the worker. Off, `/metrics` answers 404. Set `METRICS_TOKEN` too; scrapers must then send `Authorization: Bearer <token>`, and other requests get 401. Without a token, anyone who can reach the port can read the metrics.

Scrape from inside your network, not through the public proxy. Leave `/metrics` out of the proxy's routes. With Compose, run Prometheus on the same Compose network and scrape `api:8000` and `worker:9100`; use DNS service discovery so every replica is scraped:

```yaml
scrape_configs:
  - job_name: checkcord
    authorization:
      credentials_file: /etc/prometheus/checkcord-token
    dns_sd_configs:
      - names: [api]
        type: A
        port: 8000
      - names: [worker]
        type: A
        port: 9100
```

Labels never hold a person, a key, an organization, free text or record content. Routes are templates such as `/api/v1/graph/{repo_id}`.

| Metric | Type | Labels | What it counts |
|---|---|---|---|
| `checkcord_http_requests_total` | counter | `route`, `status` | API requests |
| `checkcord_http_request_duration_seconds` | histogram | `route` | API request latency |
| `checkcord_events_ingested_total` | counter | `source` | Events saved by the worker |
| `checkcord_events_skipped_removed_total` | counter | `platform` | Events not stored because an admin removed the same item before |
| `checkcord_stream_messages_total` | counter | `stream`, `group`, `outcome` | Messages each pipeline stage handled: `ok`, `failed` or `dead_letter` |
| `checkcord_pipeline_stage_total` | counter | `counter`, `stage`, `platform`, `error` | Named pipeline successes and failures, such as `chat_scrubber_error_total` |
| `checkcord_events_unfinished` | gauge | `state` | Events not yet embedded: `pending` or `failed` |
| `checkcord_stream_lag` | gauge | `stream`, `group` | Entries a consumer group has not read |
| `checkcord_stream_pending` | gauge | `stream`, `group` | Entries read but not acknowledged |
| `checkcord_dead_letter_entries` | gauge | `stream` | Entries in each dead-letter stream |
| `checkcord_chat_open` | gauge | `kind` | Chat channel buffers (`buffers`) and discussions (`discussions`) still collecting messages, not yet handed to extraction |
| `checkcord_chat_discussions_waiting` | gauge | none | Chat discussions waiting for extraction, including those retried after a model error |
| `checkcord_chat_discussions_failed` | gauge | none | Chat discussions parked after five unusable extraction replies; see `checkcord events failed` |
| `checkcord_model_calls_total` | counter | `kind`, `purpose`, `model`, `outcome` | Chat, embedding and rerank calls, `ok` or `failed` |
| `checkcord_model_tokens_total` | counter | `kind`, `purpose`, `model`, `direction` | Tokens reported by the provider, `input` or `output` |
| `checkcord_audience_containers_stale` | gauge | none | Stored audiences not read within `AUDIENCE_REFRESH_GRACE_HOURS` |
| `checkcord_scheduled_job_last_success_timestamp_seconds` | gauge | `job` | When each scheduled job last succeeded, on any process |
| `checkcord_scheduled_job_last_failure_timestamp_seconds` | gauge | `job` | When each scheduled job last failed |
| `checkcord_auth_failures_total` | counter | `channel` | Rejected keys and sessions |
| `checkcord_sign_in_failures_total` | counter | `reason` | Refused browser sign-ins |
| `checkcord_audit_write_failures_total` | counter | none | Audit entries that could not be written |
| `checkcord_rate_limited_total` | counter | `limit` | Requests answered 429 |
| `checkcord_rate_limit_redis_errors_total` | counter | none | Rate-limit checks that could not reach Redis |
| `checkcord_provider_rate_limited_total` | counter | `provider` | 429 answers from Slack, Microsoft Graph, GitHub, Jira, Notion or Linear that Checkcord waited on |

Counters are per process; sum them across replicas. The gauges are read from Postgres and Redis at each scrape and are the same on every process, so take `max`, not `sum`.

### Alerts

Start with these five:

```yaml
groups:
  - name: checkcord
    rules:
      - alert: CheckcordDown
        expr: up{job="checkcord"} == 0
        for: 5m
      - alert: CheckcordPipelineBacklog
        expr: max by (stream, group) (checkcord_stream_lag) > 1000
        for: 15m
      - alert: CheckcordDeadLetters
        expr: max by (stream) (checkcord_dead_letter_entries) > 0
        for: 10m
      - alert: CheckcordScheduledJobLate
        expr: time() - max by (job) (checkcord_scheduled_job_last_success_timestamp_seconds{job=~"refresh_audiences|renew_teams_meeting_subscriptions"}) > 3 * 3600
        for: 10m
      - alert: CheckcordModelFailures
        expr: sum(rate(checkcord_model_calls_total{outcome="failed"}[10m])) / sum(rate(checkcord_model_calls_total[10m])) > 0.2
        for: 15m
```

`CheckcordDown` fires when a process stops answering scrapes. Pair it with your orchestrator's readiness alerts. Watch `checkcord_audit_write_failures_total`, `checkcord_audience_containers_stale` and `checkcord_events_unfinished{state="failed"}` too; any rise needs a look.

## Admin API

Everything the `checkcord` command manages is also on the API under `/api/v1/admin/`. It needs an admin key, or the session of a person whose admin flag is set. A browser session must send the `X-CSRF-Token` header on changes; see [sign-in.md](sign-in.md#requests-from-the-browser). Each change is audited. Answers never contain a key or a secret; a new key is shown once.

| Area | Endpoints |
|---|---|
| Keys | `GET /keys`, `POST /keys`, `POST /keys/{id}/revoke` |
| People | `GET /users`, `GET /users/{id}`, `PUT /users/{id}/admin`, `POST /users/{id}/deactivate`, `POST /users/{id}/reactivate`, `DELETE /users/{id}` |
| Sessions | `GET /sessions`, `POST /sessions/{id}/revoke` |
| Connections | `GET /connections`, `POST /connections/review`, `GET` and `PUT /connections/scope`, `POST /connections/disconnect`, `POST /connections/install-link`, `POST /repos/connect` |
| Audiences | `GET /audiences`, `POST /audiences/refresh` |
| Failed events | `GET /events/failed`, `POST /events/retry`, `POST /events/{id}/acl-override` |
| Dead letters | `GET /dlq`, `GET /dlq/{stream}/entries`, `POST /dlq/{stream}/replay`, `POST /dlq/{stream}/drop` |
| Records | `GET /records` (`state=live`, `held` or `retired`), `GET /records/{id}`, `DELETE /records/{id}`, `PATCH /records/{id}/scope`, `PATCH /records/{id}/severity`, `POST /records/{id}/retire`, `POST /records/{id}/restore`, `GET /raw-content/{node_id}` |
| Replacements | `GET /records/suggestions`, `POST /records/suggestions/{id}/accept`, `POST /records/suggestions/{id}/dismiss` |
| Review | `GET /review`, `POST /review/release`, `POST /review/reject`. Each change takes `connection`, `record_id` or `record_ids` (up to 500) |
| Purges | `POST /purge/source`, `POST /purge/event`, `POST /purge/record`. `GET /purge/removed` counts removed items remembered; `POST /purge/removed/clear` with an optional `platform` forgets them |
| Reports | `GET /audit`, `GET /audit/export`, `GET /usage`, `GET /stats`, `GET /health` |

`GET /api/v1/graph/repositories` lists the repositories that have a code graph, with their file counts. Any key or session can call it.

Lists return `{"items": [...], "next_cursor": ...}`. Pass `cursor=<next_cursor>` for the next page, and `limit` for the page size. Errors return `{"detail": ...}`. Deletes take `dry_run=true` to return the counts only. Each organization sees only its own data. Dead-letter lists show the caller's entries only; `GET /health` counts the caller's organization only.

With `API_DOCS_ENABLED`, `/docs` shows every endpoint with its request and response.

## Audit log

Checkcord records every read of record content, every admin action and failed sign-ins in the `audit_log_entries` table. [security.md](security.md#what-is-enforced) lists what is recorded. Search text that agents send is stored in the entry, cut to 200 characters.

Read it with the `checkcord` command:

```bash
docker compose exec api checkcord audit list --user alice@example.com --since 2026-01-01
docker compose exec api checkcord audit export --since 2026-01-01 --until 2026-02-01 --format csv > audit.csv
```

Or with the admin API:

| Endpoint | What it returns |
|---|---|
| `GET /api/v1/admin/audit` | Entries, newest first. Query parameters `since`, `until`, `user`, `key`, `action`, `channel`, `record_id`, `limit` (up to 1000) and `cursor`. Pass the `next_cursor` from one page to get the next |
| `GET /api/v1/admin/audit/export?since=&until=&format=jsonl` | Every entry in the range, oldest first, as JSON Lines or `format=csv`. Takes the same filters as the list |

Reading or exporting the audit log is recorded too.

### Shipping audit logs

Each entry is also written as one JSON log line with `"logger": "checkcord.audit"` and `"event": "audit"`, on standard output of the `api` container, or standard error for `checkcord` commands. The line has the same fields as the table row and no record text. A line is written only after the entry is committed.

To send them to your log store, collect the container logs with your usual agent, for example Fluent Bit, Vector or the Docker logging driver, and keep the lines where `logger` is `checkcord.audit`. Keep `ENVIRONMENT=production` so the lines are JSON. Set `AUDIT_LOG_LINES=false` to stop the lines; the table is written either way.

## Usage and cost

Checkcord counts every chat, embedding and rerank call: purpose, model, calls, failures, tokens when the provider reports them, and latency. Counts are kept per organization, day, purpose and model in the `model_usage` table. No prompt or reply text is kept. The worker and API write them about once a minute.

```bash
docker compose exec api checkcord usage --days 7
```

Or `GET /api/v1/admin/usage?days=N` with an admin key.

Checkcord does not know your prices. To see estimated cost, set `MODEL_PRICES` to the price per million input and output tokens of each model:

```bash
MODEL_PRICES={"gpt-4o-mini": [0.15, 0.6], "text-embedding-3-small": [0.02, 0]}
```

Estimates use the reported tokens only. Calls whose provider reports no tokens, such as rerank, count as calls only.

For an activity summary, run `checkcord stats --days N`, or `GET /api/v1/admin/stats?days=N` with an admin key. Dead-letter counts cover only your organization. For each source and arrival day, `messages` counts the chat messages kept (stored rows whose source id starts with `chat-message:`) and `events` counts everything else stored, not counting the outcome events Checkcord derives from finished chat discussions. Pull-request checks count those posted, those with at least one conflict, those that failed on a blocking record, and those that ended in error.

## Reviewing a new source

Put a source in review to see what Checkcord makes of it before agents can read it.

1. Connect it in review: `checkcord connect jira --host acme.atlassian.net --review`. For Slack, Teams, Notion and Linear, run `checkcord connections review slack:TEAM_ID on` before you install. For the push API, run `checkcord connections review custom:push on`. To start every new connection in review, set `REVIEW_NEW_CONNECTIONS=true`.
2. Let it ingest. Held events go through the pipeline as usual, but their records stay held. A held record never merges with a live one.
3. Look: `checkcord review list --connection jira:acme.atlassian.net`. Summaries are redacted. `checkcord doctor` shows how many records are held.
4. Release one record with `checkcord review release --record UUID`, or delete it with `checkcord review reject --record UUID`.
5. When it looks right, run `checkcord review release jira:acme.atlassian.net`. Its records become readable under the usual audience rules, they are linked to code, and the context files are regenerated. The connection is live from then on. To drop everything held instead, run `checkcord review reject jira:acme.atlassian.net`.

The same actions are on the API with an admin key: `GET /api/v1/admin/review?connection=…`, and `POST /api/v1/admin/review/release` or `/reject` with `{"connection": "…"}`, `{"record_id": "…"}` or `{"record_ids": ["…"]}`. Records in a list that are no longer held come back in `not_held`. The console's Review page does the same. Each is audited.

To make a record fail pull-request checks, run `checkcord records severity UUID blocking`, or `PATCH /api/v1/admin/records/{id}/severity` with `{"severity": "blocking"}`. To turn a reference record into a decision, or back, run `checkcord records type UUID adr` (or `constraint`, `business_requirement`, `risk`, `general`), or `PATCH /api/v1/admin/records/{id}/type` with `{"type": "adr"}`.

### Making the check required

Checkcord posts its result as the commit status `Checkcord`. To stop a pull request from merging when it fails, open the repository's **Settings → Branches** (or **Rules → Rulesets**), add a rule for the default branch, turn on **Require status checks to pass before merging** and pick `Checkcord`. The status appears in the list only after Checkcord has posted it once, so open a pull request first. A required check also blocks on `error`, which Checkcord reports when it could not reach a verdict after three attempts (the model unreachable, an internal error, or no result within 15 minutes); push again or re-deliver the webhook to retry. Set `PR_CHECK_ON_ERROR=success` if you prefer not to hold merges then; the status says "Not evaluated: internal error." Branches whose name starts with `checkcord/`, the context-file updates Checkcord opens itself, always pass without a comment.

## Retiring a record

A record is live or retired. A retired record is not returned by the MCP tools, the graph API or the generated context file, and it never flags or blocks a pull request. New events never join it. Admins still see it in the console, with `checkcord records show` and on the admin API, and can restore it.

Retire one with `checkcord records retire UUID --replaced-by NEW --reason "…"`, `POST /api/v1/admin/records/{id}/retire` with `{"replaced_by": "…", "reason": "…"}`, or the record page in the console. Both fields are optional. The replacement must be a live record. Restore with `checkcord records restore UUID` or `POST /api/v1/admin/records/{id}/restore`. Both are audited, as `context.retired` and `context.restored`, and regenerate the context files.

Checkcord suggests replacements but never retires on its own. A suggestion needs a new record from a live event and an older live record in the same scope. There are two kinds:

- **Stated.** The new text says it replaces an earlier decision ("supersedes", "replaces the earlier …", "no longer applies"). The older record is the closest candidate, preferring one that names the same code. No model is asked. Such an event always starts its own record.
- **Model.** When the linking model compares an event with a candidate it answers `SAME`, `CHANGES` or `DIFFERENT`. `CHANGES` starts a new record and suggests that it replaces the candidate, if the two types are compatible: the same type, or both among `adr`, `constraint` and `business_requirement`. A risk never replaces a decision this way.

Until an admin decides, agents, the PR comment, the context file, `checkcord records show` and the record page show a note on the older record that a newer one may replace it, but only to readers who may read the newer record. Review the newer record's text before accepting: a model suggestion is a judgment, not something the author said.

Suggestions are listed by `checkcord records suggestions`, `GET /api/v1/admin/records/suggestions` and the console's Replacements page. Nothing changes until an admin accepts one, which retires the older record with the newer one as its replacement, or dismisses it. Dismissals are audited as `context.replacement_dismissed`.

## Retention and deletion

### What a purge deletes

Every way of deleting content goes through one purge:

- The selected events and their links to records.
- A record made from a deleted event. A record also goes when it has no events left.
- A record that keeps another source stays. Its audience is worked out again from the events left.
- Meeting segments and the decisions, constraints and risks extracted from them, with the events made from those.
- CI failure links. Agent ledger entries lose their link to the event.
- Chat messages waiting in Redis, and discussions not yet turned into records, for the channel, connection or person.
- When a whole source or connection goes, its stored audiences.

After a purge, the generated context files of connected repositories are regenerated. Each purge writes one `data.purged` audit entry with what was selected and the counts. It holds no content.

Large purges run in batches of 500 events.

### Removed stays removed

An item removed by a purge, a review rejection or the deletion of a person is remembered by a hash of its source id and is not stored again when a webhook, a poll or a backfill delivers it again. A later change at the source that arrives as a new item (a Jira update, a new comment) is stored, and held if the connection is in review. An edit that keeps the item's source id, such as an edited Notion page or Jira comment, is not stored. Disconnecting with `--purge` and retention remember nothing. `checkcord removed count` prints how many are remembered per platform; `checkcord removed clear` forgets them, all or one `--platform`, and writes a `removed.cleared` audit entry. Skipped deliveries are counted in `checkcord_events_skipped_removed_total`.

### Keeping events for a fixed time

Set `EVENT_RETENTION_DAYS`. Every day at 03:00 UTC the API deletes events that happened longer ago than that, through the same purge, then drops the weekly `events` partitions that are older and empty. Unset, events are kept forever. `checkcord doctor` prints the retention in effect.

### Deleting a person

`checkcord users delete EMAIL`, or SCIM `DELETE /scim/v2/Users/{id}`:

- deletes their API keys and sessions, so they can no longer sign in;
- deletes the events they wrote in every source, matched by their user id, by their linked chat and tool accounts, and by an author email equal to theirs (case does not matter);
- removes their id from every stored audience: events, records and channel or repository audiences;
- deletes their chat accounts and repository usage, then the person.

Each Jira and Linear comment is its own event under its own author, so a person's comments go with them and the ticket stays with its reporter. A Notion page has no author per paragraph: it belongs to the person who created it. Notion gives only that person's Notion user id, not their email, so remove a page someone else must not keep by purging the page or the connection.

Deactivating a person through SCIM (`active=false`) or `POST /api/v1/admin/users/{id}/deactivate` stops their keys and ends their sessions at once. It deletes nothing.

Past audit entries are kept as they are, so entries made before the deletion can still name the person. The `user.deleted` entry holds their id, not their email. To remove old entries, prune the audit log.

`checkcord users delete` runs in its own process. A running API can accept a cached key for up to five minutes. Sessions end at once. SCIM `DELETE` and `DELETE /api/v1/admin/users/{id}` take effect at once.

### Pruning the audit log

The app database role cannot delete audit entries. Only the schema owner can. `checkcord audit prune` connects with `MIGRATION_DATABASE_URL` and deletes entries older than `AUDIT_RETENTION_DAYS`, or `--days N`. It writes one `audit.pruned` entry per organization with the count. Run it on a schedule if you want a rolling window:

```bash
docker compose exec api checkcord audit prune --yes
```

### What deletion does not reach

- Database backups. Keep backups for no longer than your retention.
- Redis append-only files, until Redis rewrites them.
- Text already posted in pull-request comments.
- The git history of generated context files.
- Anything the model provider kept.

## Upgrading

Back up first, and read the [changelog](../CHANGELOG.md) for every version in between. Then, if you build here:

```bash
git pull
docker compose up -d --build --wait
```

If you run published images, check out the new version's tag, change the tag in `CHECKCORD_IMAGE` and `CHECKCORD_WEB_IMAGE`, then:

```bash
docker compose pull
docker compose up -d --wait
```

Migrations run forward only. To go back, restore the backup taken before the upgrade, then set the old tag.

The `migrate` service applies new migrations to the existing database before the API and worker start. Until it has, their readiness reports the schema as behind. Before 1.0 a minor version can contain breaking changes.

## Backup and restore

### What to back up

- **Postgres.** It holds everything: events, contexts, the code graph, people, keys, connector credentials (encrypted) and the audit log.
- **`FERNET_MASTER_KEY`.** Keep it apart from the dump. A dump without the key cannot decrypt connector credentials, and a dump with the key next to it protects nothing.
- **`.env`**, or wherever your settings live. The database passwords in it must match the roles in the dump.

Redis holds events that are queued but not processed yet, and rate-limit counters. It writes them to its volume so a restart keeps them. The worker removes stream entries once every consumer group has acknowledged them. Losing the volume loses only that backlog; connected systems that poll will catch up, but webhook events received in that window are gone. You do not need to back it up.

### Back up

```bash
docker compose exec -T postgres pg_dump -U checkcord -d checkcord -Fc > checkcord-$(date +%F).dump
```

`-T` keeps the dump binary-safe. The dump holds the schema, the data, the row-level security policies and the grants to `checkcord_app`. It does not hold the roles themselves or their passwords; those come from `.env` when the database is created.

### Restore

Restore into a new, empty database with the same `.env`. The dump can come from this Checkcord version or an older one.

```bash
docker compose down -v                      # only when you mean to replace the data
docker compose up -d --wait postgres redis
docker compose exec -T postgres pg_restore -U checkcord -d checkcord --exit-on-error < checkcord-2026-01-31.dump
docker compose up -d --wait
docker compose exec api checkcord doctor
```

On its first start the `postgres` container creates `checkcord_owner` and `checkcord_app` with the passwords in `.env`. `pg_restore` runs as the superuser, so the tables keep `checkcord_owner` as their owner and `checkcord_app` keeps the grants it had. Then `migrate` brings an older dump up to this version.

Roles you created yourself, such as a read-only reporting role, are not in the dump either. Create them in the new cluster before you restore: `--exit-on-error` stops at the first grant to a role that does not exist.

Check after a restore:

- `checkcord doctor` shows `PASS` for the database role, migrations, admin key, `stored credentials` (which decrypts each credential with `FERNET_MASTER_KEY`) and `audit grants`.
- An existing API key still works.
- `docker compose exec postgres psql -U checkcord -d checkcord -c '\dp audit_log_entries'` shows `checkcord_app=ar`: the app role can add audit entries but not change them.

On a managed Postgres, run `deploy/postgres/roles.sql` on the new database first, then `pg_restore` as an admin user that is a member of `checkcord_owner`. This path has not been tested on a managed service. Running `roles.sql` again on an existing database is safe; it does not widen the audit grants.

## Running on Kubernetes or another orchestrator

Checkcord ships no manifests or Helm chart. Build the two images from this repository (`Dockerfile` and `web/Dockerfile`) and run four processes from them.

| Process | Image | Command | Port | Liveness | Readiness | Metrics |
|---|---|---|---|---|---|---|
| migrate | `checkcord` | `checkcord migrate` | none | none | none | none |
| api | `checkcord` | `checkcord api --host 0.0.0.0 --port 8000` | 8000 | `GET /healthz` | `GET /readyz` | `GET /metrics` |
| worker | `checkcord` | `checkcord worker` | 9100 | `GET /healthz` | `GET /readyz` | `GET /metrics` |
| web | `checkcord-web` | the image default, `node server.js` | 3000 | `GET /` | `GET /` | none |

- **migrate** runs once per release, before the new api and worker start: a Kubernetes Job, an init step in your pipeline, or a Helm-style pre-upgrade hook of your own. It needs `MIGRATION_DATABASE_URL` and exits when done. Running it twice is safe; it does nothing at head.
- **api** and **worker** need `WORKER_HTTP_HOST=0.0.0.0` (worker) and the same settings. Until migrate has finished, their readiness fails.
- **web** reads `CHECKCORD_API_URL` when it starts. It holds no state.

Both images run as a non-root user.

### Settings and secrets

Every setting is an environment variable; [configuration.md](configuration.md) lists them. Give the api, worker and migrate the same set. Keep these in your secret store:

- `FERNET_MASTER_KEY`
- `DATABASE_URL` and `MIGRATION_DATABASE_URL`, which hold the database passwords
- `OPENAI_API_KEY`, `VOYAGE_API_KEY` and the connector secrets (`SLACK_*`, `MSTEAMS_*`, `NOTION_*`, `LINEAR_*`, `OIDC_CLIENT_SECRET`, `OAUTH_STATE_SECRET`)
- `METRICS_TOKEN`

Set `ENVIRONMENT=production`, `PUBLIC_BASE_URL`, `WEB_BASE_URL` and `FORWARDED_ALLOW_IPS` (your ingress controller's addresses or pod range). See [Addresses and ports](#addresses-and-ports).

### What must persist

- Postgres, with the `vector` extension and the two roles from `deploy/postgres/roles.sql`. Use a managed database or a stateful set with a volume, and back it up.
- Redis, with append-only persistence (`--appendonly yes`) on a volume. Losing it loses only queued events; see [Backup and restore](#backup-and-restore).
- `FERNET_MASTER_KEY`, which never changes.

Nothing else persists. api, worker and web pods can be replaced at any time.

### What scales

| Process | Replicas |
|---|---|
| migrate | exactly one at a time |
| api | one or more. Scheduled jobs are locked in Postgres. See [Scaling](#scaling) |
| worker | one or more. Workers share Redis consumer groups |
| web | one or more |
| Postgres, Redis | one primary each. Checkcord does not use read replicas or Redis Cluster |

Use your orchestrator's readiness probe on `/readyz` and its liveness probe on `/healthz`. Scrape `/metrics` from inside the cluster with `METRICS_TOKEN`, and keep it off your ingress.

## Local models

Checkcord talks to any server with an OpenAI-compatible API: Ollama, vLLM, LM Studio, llama.cpp. Set `LLM_BASE_URL` and `EMBEDDING_BASE_URL` to it, set `OPENAI_API_KEY` to anything if the server ignores keys, and name the models in the `*_LLM_MODEL` settings. `.env.local-models.example` does this for Ollama.

From inside the containers the host is `host.docker.internal`. Compose maps that name on Linux too. On Linux, Ollama listens on 127.0.0.1 by default, which containers cannot reach. Set `OLLAMA_HOST=0.0.0.0` for the Ollama service (`systemctl edit ollama`, then restart it). Keep port 11434 closed to other machines with your firewall.

`checkcord doctor` checks that the endpoint lists every configured model and that the embedding model answers at the configured width.

Measured on a laptop (Apple M1 Pro, 16 GB), with `mxbai-embed-large` for embeddings:

| Model | Result | Time per call |
|---|---|---|
| `qwen2.5-coder:7b` | Works for every role: noise filter, linking, redaction, extraction; see the [field tests](field-tests.md) for the mistakes it makes. Sometimes redacts a function name next to the word "secret" | 0.5 to 11 s; 2 to 4 s per pushed event end to end |
| `llama3.2:3b` | Runs every role, but its verdicts are unreliable: it called a clear duplicate "different", and its meeting judge rejected a decision the transcript supported. Not recommended for `LINKAGE_LLM_MODEL` or meetings | 0.1 to 6 s |
| `deepseek-r1:14b` | Correct, but slow. Needs `LLM_TIMEOUT_SECONDS` of 120 or more | 15 to 60 s |

Hosted models are more accurate at merging duplicates and at extraction from long meetings. Use local models to try Checkcord and for data that must not leave the machine; compare with a hosted model before you rely on the results.

Replies from small and reasoning models are parsed leniently: `<think>` blocks are ignored, JSON is found inside surrounding text, and a reply that is neither yes nor no is asked once more. A linking verdict that is still unclear fails the event, which is retried later; it never guesses.

## Changing the embedding model

The embedding column holds vectors of one width, `EMBEDDING_DIMENSION`. `mxbai-embed-large` and Voyage's default produce 1024, OpenAI's `text-embedding-3-small` 1536.

The worker and the API refuse to start when the column and `EMBEDDING_DIMENSION` disagree, and the worker refuses when the model answers with another width. Each message says what to run.

To change the width:

```bash
docker compose run --rm migrate checkcord migrate --reset-embeddings
docker compose up -d
docker compose run --rm -v ./scripts:/app/scripts:ro worker python scripts/reembed_canonical_contexts.py
```

`checkcord migrate` resizes the column when it is empty. If records already have vectors it stops and asks for `--reset-embeddings`, which clears them so the column can change. Re-embed afterwards; search finds nothing until then. Above 2000 dimensions there is no vector index and search scans the table.

Changing the model at the same width also needs a re-embed, because vectors from two models cannot be compared.

## Troubleshooting

| Symptom | Check |
|---|---|
| A service is unhealthy | `docker compose logs <service>`; then `checkcord doctor` |
| `api` or `worker` stops at start with `holds 1024-wide vectors but EMBEDDING_DIMENSION is ...` | The column and the setting disagree. See [Changing the embedding model](#changing-the-embedding-model) |
| `checkcord doctor` fails `llm models` with `ConnectError` | The containers cannot reach the model server. With Ollama on Linux, set `OLLAMA_HOST=0.0.0.0`. Check `LLM_BASE_URL` uses `host.docker.internal`, not `localhost` |
| `checkcord doctor` fails `llm models` with `does not list` | A configured model is not on the server. Pull it (`ollama pull NAME`) or fix the `*_LLM_MODEL` setting |
| `checkcord events failed` shows `the linkage model gave no verdict within 60s` | The model is too slow for `LLM_TIMEOUT_SECONDS`. Raise it, or use a smaller model for `LINKAGE_LLM_MODEL` |
| An MCP search returns `"results": []` for records you can see in the console | Records linked to code are scoped to their repository. Pass `repo_identifier`, such as `brambleway/dispatch-api` for the sample data |
| `docker compose up` fails with `port is already allocated` | Another program uses 8000 or 3000. Set `CHECKCORD_API_PORT` or `CHECKCORD_WEB_PORT`. See [Addresses and ports](#addresses-and-ports) |
| Events are accepted but nothing appears | The worker is not running, or a model or embeddings key is wrong. Run `checkcord doctor`. Look for `pipeline.event_retry_scheduled` or `pipeline.process_error` in `docker compose logs worker` |
| `checkcord doctor` warns that events are waiting to retry | The embedding call is failing. The events are saved. Fix the key or wait for the provider; the worker retries for 24 hours |
| `checkcord doctor` warns that a discussion has waited after a failed extraction | The chat extraction model is unreachable or keeps failing; the warning quotes the error. Discussions are retried with backoff until it answers |
| `checkcord doctor` warns that chat messages are waiting to be grouped | The chat router model is unreachable or slow. Messages stay in the channel buffer and are grouped once it answers; `checkcord stats` shows how many and the oldest |
| `checkcord doctor` warns about failed events | Run `checkcord events failed` to see the error. Fix the cause, then run `checkcord events retry --all` |
| `checkcord doctor` warns about dead letters | Messages failed repeatedly in a later stage. Run `checkcord dlq list`, then `checkcord dlq show STREAM`. Fix the cause, then `checkcord dlq replay STREAM --all`, or `checkcord dlq drop` to discard them |
| The API answers 429 | A rate limit was reached. `Retry-After` says when to try again, and `checkcord_rate_limited_total` shows which limit. Behind a proxy, check `FORWARDED_ALLOW_IPS` |
| `/readyz` says `schema at ..., head is ...` | `migrate` has not finished or failed. Run `docker compose logs migrate` |
| A webhook returns 401 | The secret on the provider side does not match. Run the matching `checkcord connect` command again and paste the new values |
| The API refuses to start in production | The message names the setting to fix: the encryption key, a database password, the database role, or a sign-in setting |
| Sign-in is refused | The console sign-in page says why: an unverified or unknown email, a deactivated person, or a token check. `checkcord audit list --action auth.sign_in_refused` shows the reason. `checkcord doctor` checks that the issuer answers |

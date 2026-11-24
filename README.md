# Checkcord

Checkcord gives AI coding agents the decisions your company has already made, and lets IT control who and what can read them.

Engineering decisions live in chat threads, tickets, meeting transcripts and pull-request reviews. Coding agents see none of that, so they rebuild what was rejected last quarter and break rules nobody wrote in the repository. Checkcord connects to those systems, extracts the decisions, constraints and risks, links each one to the code it concerns, and serves it to agents and to pull requests. Every item keeps its sources and the access rules of the place it came from.

You host it yourself. Company data stays in your Postgres database. The only outbound calls are to the systems you connect and to the model provider you configure.

> **Status: early preview (0.1).** The pipeline, connectors and access model are covered by tests, but no connector has been verified against a live system yet. See [Project status](#project-status), and read [what is and is not enforced](docs/security.md) before you put real data in it.

## What it does

- **Connects to where decisions are made.** Slack, Microsoft Teams (chat and meetings), GitHub (code and CI), Jira, Notion, Linear, Sentry, Datadog and Zoom, plus a signed webhook and a push API for anything else. Each connector carries a [status label](docs/connectors.md#connector-status).
- **Turns conversation into context.** Filters noise, groups related messages, extracts decisions, constraints and risks, and merges duplicates across sources into one record with an evidence score that decays unless something refreshes it.
  - Records are of two kinds. Decision records (`adr`, `constraint`, `business_requirement`, `risk`) are extracted from chat discussions and meeting transcripts. Everything else (pull requests, tickets, pages and alerts) is kept as reference records (`general`) that agents can search. A decision written in a ticket, a pull request or a page is stored as a reference record and is not extracted; an admin can change a record's type.
- **Links context to code.** Parses Python, TypeScript, JavaScript, Go and Java, and attaches each record to the classes and functions it is about.
- **Serves agents over MCP.** Three tools: decisions in scope for a repository, team or path; semantic search; the symbols in a file. Every result cites its sources.
- **Checks pull requests.** Posts a `Checkcord` commit status and one comment per pull request. By default a model reads the diff against each blocking record in scope for the repository and each record attached to a changed function; the check fails only when the model says the change conflicts with a blocking record. The model can be wrong in both directions, and a change it cannot place passes. If the model cannot be reached, the status stays pending through three tries a minute apart, then reports an error. With `PR_CHECK_MODE=touch` no model is called: the comment lists records attached to changed code, and a blocking one fails the check. Records are advisory until an admin marks one blocking with `checkcord records severity`.
- **Retires what no longer holds.** An admin retires a record with `checkcord records retire` or the console, and agents, context files and checks stop using it. When a new record says it replaces an older one, Checkcord suggests the replacement; nothing changes until an admin accepts.
- **Keeps IT in control.**
  - Each connection can include or exclude channels, repositories, projects and pages. Excluded material is dropped before it is stored or sent to a model.
  - Row-level tenant isolation in Postgres.
  - A record has the audience of the event it was made from, and an event joins it only if that does not widen who can read it. A private follow-up on a public decision is shown only to those who can read the follow-up. Reads are filtered per person.
  - Jira, Notion, Linear, Sentry and Datadog say nothing about who may read what they send, so an admin sets it per connection or project with `checkcord connections audience`; until then nobody can.
  - Personal data is redacted when a record is stored, and again by pattern on every read. Reads never call a model. Unredacted reads need an admin key and are logged.
  - Model calls and tokens are counted per organization, with cost estimates when you set prices. `checkcord usage` and `checkcord stats` show them.
  - Users and groups are provisioned through SCIM 2.0 ([provisioning.md](docs/provisioning.md)), and a SCIM group can grant admin rights. People can sign in through your identity provider with OpenID Connect (Okta, Entra ID, Google, Keycloak), with server-side sessions.
  - Everything the `checkcord` command manages is also on an admin HTTP API under `/api/v1/admin/`, and in a web console: connections, review, records, people, keys, audit, audiences and the pipeline. People without admin rights see the context graph only.
  - Every read of record content and every admin action is written to an audit log, in the same transaction as the read, with the key, the person and the record ids. Export it as JSON Lines or CSV, or ship it as log lines.
  - Rate limits per key, session, address and connection, Prometheus metrics, and readiness checks. The API and worker can run as several replicas.

The name comes from a check cord: a long training lead that lets a dog work freely while the handler keeps control.

## Quick start

This runs Checkcord with models on your own machine, loads sample data for an invented company, and has an agent read a record over MCP. No API key is needed and nothing leaves the machine. It takes about 15 minutes, most of it downloading models and images.

You need Docker with Compose, and [Ollama](https://ollama.com) running on the host with two models (about 5 GB):

```bash
ollama pull qwen2.5-coder:7b
ollama pull mxbai-embed-large
```

On Linux, Ollama must accept connections from containers: set `OLLAMA_HOST=0.0.0.0` for its service and restart it.

**1. Configure and start** (the first build took about 80 seconds on a laptop, plus downloading base images; later starts under 30 seconds):

```bash
git clone https://github.com/mihirp381/checkcord.git checkcord && cd checkcord
cp .env.local-models.example .env
docker compose up -d --build --wait
```

The API listens on port 8000 and the console on 3000. If either is taken, uncomment `CHECKCORD_API_PORT` or `CHECKCORD_WEB_PORT` in `.env`, change `PUBLIC_BASE_URL` and `WEB_BASE_URL` to match, run the command again, and use your ports below.

**2. Check it:**

```bash
docker compose exec api checkcord doctor
```

Every line should start with `PASS`, except `FAIL admin key` until you run step 3, and a `WARN` for the encryption key, which development mode allows. The lines that matter for local models:

```
PASS  embedding column   vector(1024)
PASS  llm models         http://host.docker.internal:11434/v1 lists qwen2.5-coder:7b
PASS  embedding endpoint mxbai-embed-large: answered with 1024-wide vectors
```

**3. Create the admin key** (printed once; keep it):

```bash
docker compose exec api checkcord init
```

**4. Load the sample data:**

```bash
docker compose exec api checkcord demo load
```

Twelve events from "Brambleway Freight", an invented company, go through the real pipeline: noise filtering, redaction, embedding, merging, and linking to code. This took about 20 seconds on a laptop. The command waits, then prints two people's keys, a `curl` command and the configuration for an agent.

**5. Read a record as an agent.** Run the `curl` command the demo printed. It asks the MCP server, as Mara Quist, for records about the carrier webhook secret in the `brambleway/dispatch-api` repository. Records are scoped to a repository, so an agent names the one it works in. Run it again with Pell Okonjo's key: the two records from the private `#security-review` channel are missing, because Pell is not in that channel.

**6. Look in the console.** Open `http://localhost:3000`, choose **Continue with an API key**, and paste the admin key.

### What you should see

- [ ] **Overview**: health checks pass; Model usage shows calls to `qwen2.5-coder:7b` and `mxbai-embed-large`.
- [ ] **Records**: about ten records starting with `[Sample]`. An email address and a phone number show as `[REDACTED:EMAIL]` and `[REDACTED:PHONE]`.
- [ ] **Review**: two vendor-wiki records held from the `custom:sample-vendor-wiki` connection. Release one and reject the other.
- [ ] **Replacements**: the live-quote decision is suggested as the replacement for the nightly batch one. Accept it, and the nightly batch record is retired: agents no longer get it.
- [ ] **Audiences**: `#dispatch-eng` is public; `#security-review` has two members.
- [ ] **Graph**: repository `brambleway/dispatch-api`, with records attached to functions such as `fetch_live_quote`.
- [ ] **Audit**: an `mcp` entry for each `curl` call, with the person, the key and the record ids returned. `docker compose exec api checkcord audit list` shows the same.

Remove the sample data with `docker compose exec api checkcord demo remove`. Stop everything and delete the data with `docker compose down -v`.

Small local models are slower and less accurate than hosted ones. They can miss a duplicate, or redact a function name next to the word "secret". See [local models](docs/self-hosting.md#local-models) for which models worked in the field tests.

### With a hosted model provider

For real data, use production mode and a hosted provider:

```bash
cp .env.example .env
```

Edit `.env`: set `OPENAI_API_KEY` and `VOYAGE_API_KEY`, then generate `FERNET_MASTER_KEY` and the three database passwords as the comments in the file show. Checkcord refuses to start in production mode with a default password. Then run steps 1 to 3 above, skipping the `cp`. `checkcord demo load` works here too.

Next steps:

1. [Connect your systems](docs/connectors.md).
2. [Connect a repository](docs/connectors.md#github) so records can be linked to code.
3. [Give each developer a key and point their agent at Checkcord](docs/agent-setup.md).

[docs/self-hosting.md](docs/self-hosting.md) covers production settings, scaling, monitoring, backups and upgrades. If something goes wrong, see [troubleshooting](docs/self-hosting.md#troubleshooting).

### From published images

Each release publishes the API and console images for amd64 and arm64. To run them instead of building, check out the release tag, set `CHECKCORD_IMAGE` and `CHECKCORD_WEB_IMAGE` in `.env`, and run `docker compose pull` then `docker compose up -d --wait`. See [images](docs/self-hosting.md#images).

## Project status

What is tested:

- Every connector, the pipeline, the access model, the admin API, SCIM and sign-in have unit tests, and integration tests against real Postgres and Redis.
- The console has component tests.
- The quick start above runs end to end on local models.
- The whole product has run against a simulated company on a local model: 47 scenarios, from one clear decision in a public channel to redeliveries, crashes, planted instructions and people probing for access. [Field tests](docs/field-tests.md) reports every scenario, what it checks and what is still weak.

What has never run against the real thing:

- **Connectors.** Each one is tested against recorded and hand-written payloads, not a live Slack, Teams, GitHub, Jira, Notion, Linear, Sentry, Datadog or Zoom account. Their labels are in [connector status](docs/connectors.md#connector-status). If you run one, please file a [connector verification report](.github/ISSUE_TEMPLATE/connector_verification.md).
- **Identity providers and SCIM clients.** Okta, Entra ID, Google and Keycloak sign-in, and SCIM from Okta or Entra ID, are tested against the protocols, not the products.
- **Hosted models.** Tests use stubs. Only local models ran end to end.
- **Deployment.** Checkcord has not run on Kubernetes or on a managed Postgres.

What is known to be weak:

- Small local models. They are slow and their judgement varies from run to run. Use a hosted model for real data.
- Meeting transcripts on a small model: they can produce a junk record and miss the link between meetings that contradict each other. Keep meetings in review.
- A decision buried in a very long message is missed, and a decision with an end date stays in force until an admin retires it. Both are described in the [field tests](docs/field-tests.md).

## Documentation

| | |
|---|---|
| [Self-hosting](docs/self-hosting.md) | Install, configure, scale, monitor, upgrade, back up and restore |
| [Configuration](docs/configuration.md) | Every setting |
| [Connectors](docs/connectors.md) | Status of each connector, and setup for each connected system |
| [Agents](docs/agent-setup.md) | MCP setup, keys per developer, the prompt proxy |
| [Sign-in](docs/sign-in.md) | Browser sign-in with Okta, Entra ID, Google or Keycloak |
| [Provisioning](docs/provisioning.md) | SCIM users and groups from Okta or Entra ID |
| [Security model](docs/security.md) | What is enforced, what is not, what leaves your network |
| [Architecture](docs/architecture.md) | Processes, pipeline, data model |
| [Field tests](docs/field-tests.md) | How it behaves against a simulated company, scenario by scenario |
| [Extending](docs/extending.md) | Pushing events, writing a connector, the HTTP API, adding an MCP tool |
| [Examples](examples/) | Scripts that push events, send a signed webhook and read over MCP |
| [Releasing](docs/releasing.md) | How a maintainer cuts a release |
| [Changelog](CHANGELOG.md) | What changed in each version |

## Development

```bash
make setup      # dependencies and git hooks
make dev-up     # Postgres and Redis with migrations applied
make test       # fast tests, no services needed
make test-integration
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities as [SECURITY.md](SECURITY.md) describes; the [code of conduct](CODE_OF_CONDUCT.md) applies to everyone taking part.

## Licence

Apache License 2.0. See [LICENSE](LICENSE).

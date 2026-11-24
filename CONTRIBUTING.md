# Contributing

Thanks for helping. This page covers how to get a working checkout, what a change needs before review, and how the code is laid out.

## Set up

You need Python 3.12 or newer, [uv](https://docs.astral.sh/uv/), Docker, and Node 20.9 or newer for the console (CI uses 22).

```bash
make setup      # install Python dependencies and git hooks
make dev-up     # start Postgres and Redis, apply migrations
make test       # fast tests, no services needed
```

For the console:

```bash
cd web
npm ci
npm run lint
npm run typecheck
npm test
npm run dev
```

## Before you open a pull request

- `make lint`, `make typecheck` and `make test` pass.
- If you changed the console, `npm run lint`, `npm run typecheck`, `npm test` and `npm run build` pass in `web/`.
- If you touched anything that talks to Postgres or Redis, `make test-integration` passes with `make dev-up` running.
- New behaviour has a test. A bug fix has a test that fails without the fix.
- A schema change comes with an Alembic migration, and `uv run alembic upgrade head` works on an empty database.
- A new setting is added to `.env.example` and `docs/configuration.md`.
- A changed route comes with a regenerated `docs/openapi.json`: `uv run python scripts/export_openapi.py`.
- A change people will notice has a line in the top, undated section of `CHANGELOG.md`.

`scripts/compose_smoke.sh` runs the Compose smoke test CI runs: it builds and starts the stack on ports 18000, 13000, 15432 and 16379, checks it and pushes events. It needs no `.env` in the checkout and removes everything it made. With `SMOKE_MODEL=1` and the local models from the README it also reads the records back over MCP.

Keep pull requests to one change. Say what the change does and how you checked it.

## Simulated company tests

`sim/` runs the whole product against stand-ins for Slack, GitHub, Jira, the identity provider and the model server, with a small invented company and scripted scenarios: `uv run python -m sim up`, `uv run python -m sim run`, `uv run python -m sim down`. It needs Docker and the local models, the easy level takes about 15 to 20 minutes and both levels a little over an hour, and it is not part of CI. [docs/field-tests.md](docs/field-tests.md) reports the results of the latest complete run. [sim/README.md](sim/README.md) explains how to run it, how to add a scenario and what the stand-ins do not imitate.

## Clustering evaluation

`golden_sets/clustering.jsonl` holds labelled chat threads for checking how events are grouped into records. `uv run python scripts/eval_clustering.py --golden-set golden_sets/clustering.jsonl` reports precision, recall and ranking metrics over a range of similarity thresholds; add `--rerank` to include Voyage reranking. It makes real embedding calls with the configured provider and key, so it costs money, and it is not part of CI.

## Code style

- Ruff formats and lints the Python code; ESLint covers the web code.
- Keep comments short. Write one only where the reason is not clear from the code.
- Every query that touches tenant data runs inside `tenant_context(...)` so row-level security applies. Use `app_bypass_rls_context(...)` only for lookups that have to run before the tenant is known, such as resolving an API key.
- Never log secrets, tokens or message content.

## Where things live

| Path | What it holds |
|---|---|
| `src/checkcord/adapters/` | One package per connected system: webhooks, OAuth, polling, parsing, and its `connector.py` definition |
| `src/checkcord/connectors/` | The connector definition, the registry, setup helpers and the signed webhook connector |
| `src/checkcord/pipeline/` | Workers that filter, cluster, link and extract |
| `src/checkcord/storage/` | SQLAlchemy models and the Postgres store |
| `src/checkcord/api/` | HTTP API for ingestion, the context graph and admin |
| `src/checkcord/proxy/` | Developer-side proxy and MCP server |
| `src/checkcord/ast/` | Tree-sitter parsing for Python, TypeScript, JavaScript, Go and Java |
| `alembic/` | Database migrations |
| `web/` | The Next.js admin console: sign-in, records, review, connections, people and the code graph |

[docs/architecture.md](docs/architecture.md) explains how these fit together.

## Adding a connector

[docs/extending.md](docs/extending.md#writing-a-connector) explains how a connector is defined and registered, the rules it must follow, and walks through the signed webhook connector file by file. Before you open the pull request, check that:

- [ ] Every inbound request is verified (signature or token) before anything else is done with it.
- [ ] Each event's audience comes from the source, or the event is private with an empty audience.
- [ ] The connection's scope is applied before the event is stored or sent to a model.
- [ ] Credentials are stored encrypted, and no secret, token or message content is logged.
- [ ] The connector is registered with a `status` label that meets the criteria in [docs/connectors.md](docs/connectors.md#connector-status).
- [ ] Unit and integration tests use recorded or synthetic payloads, never real customer data.
- [ ] `docs/connectors.md` has a row in the status table and a setup section.

## Versions and releases

The version lives in `pyproject.toml` and `web/package.json`, and the two must match. `checkcord --version`, the OpenAPI document and the console footer read it from there. Versions follow [semantic versioning](https://semver.org); before 1.0 a minor version can break things, and the changelog says how.

A release is a tag `vX.Y.Z` on `main` whose version matches both files and has a dated `## [X.Y.Z] - YYYY-MM-DD` section in `CHANGELOG.md`. `scripts/check_release.py --tag vX.Y.Z` checks all of that, and the release workflow runs it before it builds anything. Maintainers follow [docs/releasing.md](docs/releasing.md).

## Licence

By contributing you agree that your contribution is licensed under the Apache License 2.0, the licence of this project.

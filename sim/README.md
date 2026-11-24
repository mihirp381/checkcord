# Simulated company tests

`sim/` runs the whole product (API, workers, Postgres, Redis, the console's API, the CLI) against stand-ins that speak the providers' real protocols, with a real local model behind a switchable proxy. An invented company, Marrowfield Systems, has nine people, six Slack channels, three GitHub repositories and two Jira projects. [docs/field-tests.md](../docs/field-tests.md) reports the results of the latest complete run. Scenarios post messages, open pull requests and change memberships, then read back what each person's coding agent receives, what the admin sees and what the stand-ins were sent.

The runner reports what the product does. A failed check is a finding, not something to work around in the scenario.

It is a developer tool: it is not in the wheel or the Docker images and does not run in CI.

## Running it

You need Docker, and Ollama on `localhost:11434` with `qwen2.5-coder:7b` and `mxbai-embed-large`. Ports 8410, 3410 and 8900 (or `SIM_WORLD_PORT`) must be free, and there must be no `.env` in the checkout that the simulation did not write.

```bash
uv run python -m sim up                      # world, .env, stack, company
uv run python -m sim run --level easy        # or hard, all; --only E01,E05; --keep to reuse a stack
uv run python -m sim down                    # stack and volumes removed, world stopped, .env deleted
uv run python -m sim world                   # the stand-ins alone, in the foreground
```

`up` checks Docker, the models and the ports, starts the world in the background (pid and log in `sim/out/`), writes `.env` from `.env.local-models.example` with every override listed under "Settings the simulation changed" in the summary, builds and starts the Compose stack, runs `checkcord doctor` and `checkcord init`, creates the people over SCIM (half Okta-style, half Entra-style payloads), gives each a personal key, creates the SCIM groups `engineering` (everyone) and `payments-team` (Tomas, Aiko, Ingrid), installs Slack through the install link, connects `web-app` and `handbook`, and links Ingrid's GitHub login with `checkcord accounts import`. Admin rights come from the `checkcord-admins` group the IdP stand-in puts in the `groups` claim.

`run` refuses a stack that already ran scenarios unless `--keep` is given, because scenarios build on each other's records. Each run writes `sim/out/<UTC timestamp>/` with one `<ID>.json` per scenario, `ledger.json` (what the runner sent and read) and `summary.md`: a table of musts passed and failed, then each scenario's failed musts with detail and its observations. A scenario that raises is recorded as `error` with its traceback, and the table names the exception; the run goes on with the next scenario. If the admin key stops working after an error, the run says so once, stops and still writes the summary. Secrets are masked in all output. The admin key is in `sim/out/admin.key` (mode 0600) while the stack is up.

`up` takes about a minute once the images are built; a full easy run takes about 15 to 20 minutes and the hard level about an hour, mostly waiting on the local model. Hard scenarios that depend on easy ones (E02, E05, E07, E20) report a failed must or an observation when run alone; `--level all` runs them in order.

Some hard scenarios change the stack while they run and put it back in a `finally`: H13 kills and starts the `worker` container, H22 writes `docker-compose.override.yml` for two API and two worker replicas, H23 adds `SCIM_ADMIN_GROUPS` to `.env` and recreates the API. `summary.md` shows, for the first three records of each scenario, what went in (the source events) and what came out (the record text).

## How a scenario is written

Scenarios live in `sim/scenarios/easy.py` and `sim/scenarios/hard.py`. Each is an async function registered with `@scenario(sid, title, last=False)`; the file sets the level, and `last=True` runs it after the others. The docstring states the setup, the musts and the observations.

```python
@scenario("E42", "A short title")
async def e42(ctx: Ctx) -> None:
    """What happens; must: what has to hold; observe: what is recorded."""
    await ctx.post("eng", "tomas", "Decision: ...")
    await ctx.settle()
    ids = await ctx.records_from("Decision: ...")
    r = await ctx.person("dev").search("...", company.PAYMENTS)
    ctx.must("Dev's agent gets it", hit(r.body, ids[0] if ids else None))
    ctx.observe("what came back", r.body)
```

- `ctx.post(channel, person, text, thread_ts=, at=, subtype=, ...)` posts through the Slack stand-in; `at` sets the provider timestamp only.
- `ctx.settle(timeout)` waits until, for two readings two seconds apart, no event is unfinished, every stream has no lag or pending entries and `checkcord_chat_open` is zero. On timeout it records a failed must "pipeline settled" and carries on.
- `ctx.person(key)` is that person's coding agent (MCP tools and graph endpoints with their key); `ctx.console(key)` is a signed-in browser session; `ctx.admin` is the admin key; `ctx.cli(...)` runs `checkcord` in the API container; `ctx.via()` alternates between the console and the CLI for admin actions.
- `ctx.world` drives the stand-ins through `/control/...`: pull requests, forged GitHub webhooks, Jira issues and redeliveries, Slack channels, redeliveries and late parents, memberships, Zoom meetings, Notion pages, Linear issues, the model mode.
- `ctx.must(name, passed, detail)` and `ctx.observe(name, value)` are the only outputs. An exception becomes status `error` with its traceback and the run continues.

Per scenario the runner also records what was sent, how long each settle took, every record created or touched as the admin sees it, what each person received, the new audit entries, new model calls by purpose, the chat discussions waiting and failed at the end, and every request the stand-ins received.

Scenarios share state through `ctx.shared` (for example the E02 record id, which later scenarios check stays out of pull-request comments and the context file). `ledger.json` is the runner's own count of events sent, agent reads and readers, pull-request checks and scope skips, which E19 compares with the product's numbers.

## What the stand-ins imitate

All of them run in one aiohttp server, record every request (method, path, body digest and body) and paginate with small pages.

| Stand-in | Imitates | Does not imitate |
|---|---|---|
| Slack | OAuth v2 install, Socket Mode (hello, envelopes, acks, redelivery after 3 s up to three times), the Web API methods the product calls (`auth.test`, `team.info`, `conversations.*`, `users.*`, `usergroups.list`, `apps.connections.open`, `apps.uninstall`), message, edit, delete and membership events, file downloads, a 429 switch | Events API over HTTP, request signing, interactive payloads, Enterprise Grid, rate limits other than the switch, real `channel_join` semantics beyond a message and the member event |
| GitHub | The REST routes the product uses (repository, events, collaborators, user profiles, pulls and their files, commits, statuses, issue comments, refs, trees, contents, check runs), signed `pull_request` webhooks for opened, synchronize and closed, and for pull requests the product opens | Webhook retries, GraphQL, GitHub Apps and installation tokens, rate-limit headers, branch protection, merge conflicts |
| Jira | Issue search, issue and comment reads, signed `jira:issue_created`, `jira:issue_updated` and `comment_created` webhooks to one or more registered connections, a redelivery with the same `X-Atlassian-Webhook-Identifier` | Webhook retries on its own, JQL beyond the simplest filters, Atlassian Connect, ADF beyond plain paragraphs, HTTPS (a connection made with `--host http://host.docker.internal:8900` lets the poller reach it) |
| Zoom | Signed event notifications (`x-zm-signature`), `endpoint.url_validation`, `recording.transcript_completed` with a WebVTT transcript behind `download_url`, which needs the event's `download_token` as a bearer token | Server-to-server OAuth, the recordings API, other file types, retries |
| Notion | OAuth install, `POST /v1/search` sorted by last edit with cursors, block children | Webhooks, nested child blocks, databases, rich text other than plain paragraphs |
| Linear | OAuth install, GraphQL `viewer`, `issues` (newest first, `first`/`last` with page info) and issue comments | Webhooks, comment paging, filters other than `updatedAt`, rate limits |
| Identity provider | OIDC discovery, authorize, token and JWKS, with `email`, `email_verified` and `groups` claims; the user is chosen with `login_hint` | Userinfo, consent screens, refresh tokens, logout, PKCE enforcement |
| SCIM | Okta-style and Entra-style user payloads and patches, sent by the runner to the product's SCIM server | Group push, a real IdP's retry behaviour |
| Model | A proxy to `localhost:11434/v1` with modes `ok`, `down` (500), `slow:<seconds>` and `garbage` (truncated JSON); every body is logged so scenarios can check what reached the model | Provider quotas, streaming quirks, other models |
| Push API and signed webhook | Called directly by the runner with the admin key or the connection's HMAC secret | - |
| Sentry and Datadog | No stand-in: the runner posts signed Sentry alerts and token-authenticated Datadog alerts to the product's webhook routes | Sentry's installation flow, Datadog's API |
| Microsoft Teams | Admin consent install, client-credentials token, Graph users, groups, teams, channels with `membershipType` and members, Bot Framework OpenID keys and signed activities to `/api/messages` (also signed by an unknown key or unsigned), Graph subscriptions with validation and transcript notifications | Adaptive cards, chats, shared channels, membership events, throttling, real Entra consent screens |

Known differences that affect results:

- Aiko's Slack profile email differs in case from their SCIM email, and Lena's Slack email is on another domain, as in real workspaces.
- GitHub profiles show a public email for Tomas, Omar, Aiko, Rosa, Dev and Kofi; Ingrid's login is linked from a CSV at bring-up, and Lena's login stays unmatched. Lena's Slack account stays unmatched until H09 links it.
- The private-channel checks in E02 and the sensitive strings from E03 are also checked in pull-request comments (E06, E12) and the context file (E17).
- Counts are read from the API; the CLI's `stats` and `usage` text is kept as an observation.
- Webhooks are posted from the host to `localhost:8410`; the product reaches the stand-ins at `host.docker.internal`.

# Connectors

## Connector status

No connector has been verified against the live system it connects to. Each one is tested against recorded or synthetic payloads and stand-ins for the provider's API. If you run one against a real system, please file a [connector verification report](../.github/ISSUE_TEMPLATE/connector_verification.md), whether it worked or not.

Each connector has a status label, shown in `checkcord connect --help`, `checkcord connections` and the console's Connections page:

- **beta**: reads each event's audience from the source, keeps it current, honours the scope, and has unit and integration tests.
- **experimental**: misses one or more of the beta criteria.

| Connector | Status | Ingestion | Audience comes from | Audience refresh | Scope | Review | What the tests cover | Live-verified |
|---|---|---|---|---|---|---|---|---|
| [GitHub](#github) | beta | Webhook and poller | Repository visibility and collaborators | `member` and `repository` webhooks, and a schedule | Repository | `--review` | Webhook signatures and parsing, CI runs, the poller and backfill, the code graph, audience refresh, scope, pull-request checks | No |
| [Slack](#slack) | beta | Socket Mode events | Channel visibility and members | Membership events and a schedule | Channel ids | `checkcord connections review` | Listener, install, directory sync, audience refresh, scope | No |
| [Microsoft Teams](#microsoft-teams) | beta | Bot Framework messages | Channel type and members | A schedule | Team, channel or chat ids | `checkcord connections review` | Bot token check, listener, install, directory sync, audience refresh, scope | No |
| [Jira](#jira) | experimental | Webhook and poller | An admin: `checkcord connections audience`. Nobody until set | SCIM group changes and a schedule | Project keys | `--review` | Webhook secret and parsing, poller, scope | No |
| [Notion](#notion) | experimental | Poller | An admin: `checkcord connections audience`. Nobody until set | SCIM group changes and a schedule | Page or parent ids | `checkcord connections review` | Install, poller, scope | No |
| [Linear](#linear) | experimental | Poller | An admin: `checkcord connections audience`. Nobody until set | SCIM group changes and a schedule | Team ids or keys | `checkcord connections review` | Install, poller, scope | No |
| [Sentry](#sentry) | experimental | Webhook | An admin: `checkcord connections audience`. Nobody until set | SCIM group changes and a schedule | None | `--review` | Signature and parsing | No |
| [Datadog](#datadog) | experimental | Webhook | An admin: `checkcord connections audience`. Nobody until set | SCIM group changes and a schedule | None | `--review` | Token and parsing | No |
| [Zoom](#zoom) | experimental | Webhook | Meeting attendees | None | None | `--review` | Endpoint validation, signature, transcript parsing, attendee audience | No |
| [Teams meetings](#teams-meetings) | experimental | Graph change notifications | Meeting attendees | None | None | `--review` | Notifications, subscription create, renew and delete against a stand-in for Graph | No |
| [Signed webhook](#signed-webhook) | experimental | Webhook | Emails in each event | Not needed: each event carries its audience | The event's `container` | `--review` | Signature, time window, audience, scope, rotation, tenant isolation | No |
| [Push API](#push-api) | experimental | Push API | The sender's `access_control` | None | None | `checkcord connections review custom:push` | Authentication and publishing | No |

Every row has unit tests and integration tests against Postgres and Redis. `checkcord doctor` lists the connectors by status on its `connectors` line. To add one, see [extending.md](extending.md).

## Setting one up

Each connector is set up with one `checkcord connect` command, run inside the API container:

```bash
docker compose exec api checkcord connect <system> [options]
```

The command stores what Checkcord needs, encrypted, and prints exactly what to configure on the other side. Run `checkcord connections` to see what is connected and `checkcord disconnect <system> <identifier>` to remove one. Disconnecting keeps the content already ingested and prints how much is left. Add `--purge` to delete it as well: the events that came through that connection, the records made only from them, and its stored audiences. For GitHub this means every event of the repository and its code index.

Secrets can be passed as an option, as an environment variable, or typed at a hidden prompt. A secret that Checkcord generates is shown once.

Add `--review` to `checkcord connect github`, `jira`, `datadog`, `sentry`, `zoom`, `teams-meetings` or `webhook` to hold what the connection brings until an admin releases it. For GitHub this covers the first backfill. See [self-hosting.md](self-hosting.md#reviewing-a-new-source).

| System | What Checkcord reads | Records it makes | How it connects | Inbound access needed |
|---|---|---|---|---|
| [GitHub](#github) | Pull requests, reviews, comments, pushes, CI runs, source files | Reference | Access token and webhook | Yes |
| [Slack](#slack) | Channel messages and threads | Decisions, by extraction | OAuth app, Socket Mode | No |
| [Microsoft Teams](#microsoft-teams) | Channel and chat messages | Decisions, by extraction | Bot and Graph app | Yes |
| [Jira](#jira) | Issues, comments | Reference | Access token and webhook | Yes |
| [Notion](#notion) | Pages | Reference | OAuth app, polling | Only for the install step |
| [Linear](#linear) | Issues and their comments, every issue updated since the last poll | Reference | OAuth app, polling | Only for the install step |
| [Sentry](#sentry) | Issues and alerts | Reference | Webhook | Yes |
| [Datadog](#datadog) | Monitor alerts | Reference | Webhook | Yes |
| [Zoom](#zoom) | Meeting transcripts | Decisions, by extraction | Webhook | Yes |
| [Teams meetings](#teams-meetings) | Meeting transcripts | Decisions, by extraction | Graph subscription | Yes |
| [Signed webhook](#signed-webhook) | Whatever the sender signs and sends | Reference | Webhook with a shared secret | Yes |
| [Anything else](#push-api) | Whatever you send | Reference, unless `metadata.context_type` names a decision type | Push API | No |

**Decisions and reference records.** Chat discussions and meeting transcripts go through extraction: a model reports the decisions, constraints, requirements and risks they settled, and each becomes a decision record of type `adr`, `constraint`, `business_requirement` or `risk`. Everything else, a pull request, a ticket, a page or an alert, is stored as a reference record of type `general` that holds the event's text. A decision written in a ticket, a pull request or a page is therefore a reference record: it is not extracted. Agents can search reference records, but `view_active_decisions`, the context file and the pull-request check treat them as decisions only when an admin marks them blocking. An admin can change a record's type with `checkcord records type RECORD_ID adr` (or `constraint`, `business_requirement`, `risk`, `general`) or on the console's record detail.

Set `PUBLIC_BASE_URL` in `.env` to the address these systems will call. The commands print webhook and install URLs built from it.

## Choosing what is ingested

By default a connection ingests everything Checkcord can read. To narrow it, list channels, repositories, projects or pages to include or exclude:

```bash
checkcord connections scope slack:T0123ABCD exclude C0SECRET1
checkcord connections scope jira:acme.atlassian.net include PAY ENG
checkcord connections scope jira:acme.atlassian.net exclude OPS --remove
checkcord connections scope slack:T0123ABCD show
checkcord connections scope slack:T0123ABCD clear
```

- Exclude always wins.
- If the include list has anything in it, only what it lists is ingested.
- Names match without regard to case. Notion ids match with or without hyphens.

Checkcord checks the scope as material arrives, before it stores anything or sends anything to a model. A change applies to what arrives from then on, within 10 seconds on every process. What was already ingested stays. To delete it, run `checkcord purge source --platform PLATFORM --container ID`.

Events that a webhook or listener drops are counted per day. `checkcord stats` and the console's Connections page show the count. Pollers check the scope before they fetch a repository, project or page, so what they skip is never fetched and not counted.

The same is on the admin API: `GET /api/v1/admin/connections/scope?connection=…`, and `PUT /api/v1/admin/connections/scope` with `{"connection": "…", "include": […], "exclude": […]}`. A list left out is kept. Each change is audited as `connection.scope_changed`, with the lists before and after. The console's Connections page has a Scope button.

| Connector | What to list | Checked in |
|---|---|---|
| Slack | Channel ids, such as `C0123ABCD` | Listener |
| Microsoft Teams | Team ids or channel ids. For a group or one-to-one chat, the chat id | Listener |
| GitHub | The repository, `owner/name` | Webhook, CI webhook and poller |
| Jira | Project keys, such as `PAY` | Webhook and poller |
| Notion | Page ids, or the id of a page's direct parent database or page | Poller |
| Linear | Team ids or keys, such as `ENG` | Poller |
| Signed webhook | The `container` each event names | Webhook |

A GitHub connection is one repository. Excluding it, or listing something else to include, stops its pull requests, pushes, CI runs and pull-request checks. Repository visibility changes are still applied.

Sentry, Datadog, Zoom, Teams meetings and the push API have no scope. A signed webhook event without a `container` is out of scope whenever the include list is not empty. Choose what they send on their side: the Sentry projects and Datadog monitors that call the webhook, the Zoom account, the Teams users subscribed with `checkcord connect teams-meetings`, and what you push.

## GitHub

```bash
checkcord connect github --repo acme/billing
```

You are asked for an access token. Use a fine-grained token limited to that repository with these permissions: Contents (read and write), Pull requests (read and write), Commit statuses (read and write), Actions (read), Checks (read), Metadata (read). Write access to contents and pull requests is used only to open pull requests that add or update files under `.context/`. The token's owner needs write access or higher to the repository: Checkcord lists the repository's collaborators before it writes anything restricted, and GitHub allows that listing only at that level. Without it, Checkcord writes public records only.

The command prints a webhook URL and secret. Add them under the repository's **Settings → Webhooks** with content type `application/json` and the events it lists. One webhook covers pull requests and CI runs. The **Collaborator add, remove, or changed** (`member`) and **Repositories** (`repository`) events let Checkcord refresh the repository's audience as soon as it changes. Without them it waits for the next scheduled refresh.

On connect, Checkcord walks the whole repository once to build the code graph. After that it updates the graph as pull requests merge.

Pull requests whose head branch starts with `checkcord/` are Checkcord's own. They, their reviews and comments, and pushes to those branches are not stored as events, whether they arrive by webhook or by the poller; the pull request check skips them too.

Checkcord matches a collaborator to a person by the public email on their GitHub profile (`GET /users/{login}`, read at most once a day per login). It never guesses from commit emails. A collaborator with no public email, or one that matches nobody, stays unmatched until an admin links them; see [Matching accounts to people](#matching-accounts-to-people). While any collaborator is unmatched, Checkcord writes only public records into the repository.

## Slack

1. Create a Slack app for your workspace.
   - Turn on **Socket Mode** and create an app-level token with `connections:write`.
   - Bot token scopes: `channels:history`, `groups:history`, `channels:read`, `groups:read`, `users:read`, `users:read.email`, `usergroups:read`. `checkcord connect slack` prints this list too.
   - Subscribe to the bot events `message.channels` and `message.groups`, `member_joined_channel`, `member_left_channel` and `channel_converted_to_private`, plus `app_uninstalled` and `tokens_revoked`. The three membership events need no extra scope. They let Checkcord refresh a channel's audience as soon as it changes.
   - Add the redirect URL `<PUBLIC_BASE_URL>/slack/oauth_redirect`.
2. Put `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_SIGNING_SECRET`, `SLACK_REDIRECT_URI` and `SLACK_APP_TOKEN` in `.env` and restart the API.
3. Run `checkcord connect slack`. It prints an install link that works once and expires after 15 minutes. A workspace admin opens it and approves.

Checkcord reads only channels the bot has been added to. It receives messages as Slack sends the events and does not read channel history: messages from before the install, or events Slack could not deliver, are not ingested. It never posts to Slack.

Checkcord groups a channel's messages into discussions and turns each finished discussion into a record. A record from a public channel is public. A record from a private channel or a direct message is visible only to the channel's members that Checkcord can match to its users. Members are matched by the email in their Slack profile: Checkcord reads the whole directory once after install, again every `DIRECTORY_SYNC_INTERVAL_MINUTES` (default 60), and looks up a member it has not seen yet when it reads a channel. If Checkcord cannot read the member list, the record has an empty audience: only an admin override can open it. See [Who can read what](#who-can-read-what) for how audiences are kept current.

## Microsoft Teams

1. Register an app in Microsoft Entra ID and create an Azure Bot that uses it. Set the bot's messaging endpoint to `<PUBLIC_BASE_URL>/api/messages`.
2. Grant the app the Microsoft Graph application permissions it needs to read channel and chat members, groups and users, and have an admin consent to them.
3. Put `MSTEAMS_CLIENT_ID` (the bot's app id), `MSTEAMS_CLIENT_SECRET` and `MSTEAMS_REDIRECT_URI` (`<PUBLIC_BASE_URL>/msteams/oauth_redirect`) in `.env` and restart the API.
4. Run `checkcord connect teams` and have a Microsoft 365 admin open the one-time link.

Every message delivered to `/api/messages` must carry a Bot Framework token issued for your app id. Anything else is rejected. Checkcord ingests the messages Teams delivers to the bot and does not read channel or chat history: messages from before the install, or ones Teams could not deliver, are not ingested.

Records from Teams follow the same rule as Slack. Only a standard team channel is public. A private or shared channel, or a chat, is visible only to its members that Checkcord can match. The Entra directory is read after install and every `DIRECTORY_SYNC_INTERVAL_MINUTES`, and an unseen member is looked up when a channel is read. If Checkcord cannot read the channel type or the members, the record is not public and has an empty audience. Teams sends no membership events to bots, so a Teams audience changes at the next scheduled refresh.

## Jira

```bash
checkcord connect jira --host acme.atlassian.net --project-key PAY --project-key OPS
```

You are asked for an access token. The command prints a webhook URL and secret to add under **Settings → System → WebHooks**. Checkcord reaches Jira over https. With `ENVIRONMENT=development`, `--host` also takes a full `http://` base URL, such as a local stand-in; the connection is then named after that URL, such as `jira:http://localhost:8080`.

The webhook and the poller give each item the same id, so an issue seen by both is stored once: a created issue, each update to the issue itself (by its update time), and each comment. A comment is its own event under its author; an edited comment replaces the stored one. A delivery whose `X-Atlassian-Webhook-Identifier` was already seen is answered `200` and stores nothing. Linear comments are also their own events under their authors.

Jira does not say who may read an issue, so its records are readable by nobody until an admin sets an audience. See [Audiences set by an admin](#audiences-set-by-an-admin). The same holds for Notion, Linear, Sentry and Datadog.

## Notion

Create a public Notion integration with the redirect URL `<PUBLIC_BASE_URL>/api/v1/webhooks/notion/oauth_redirect`. Set `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET` and `NOTION_REDIRECT_URI`, restart the API, then run `checkcord connect notion` and open the one-time link. Checkcord polls the pages the installer shared with the integration.

## Linear

Create a Linear OAuth application with the redirect URL `<PUBLIC_BASE_URL>/api/v1/webhooks/linear/oauth_redirect`. Set `LINEAR_CLIENT_ID`, `LINEAR_CLIENT_SECRET` and `LINEAR_REDIRECT_URI`, restart the API, then run `checkcord connect linear` and open the one-time link.

Every `TASK_DOCUMENT_POLL_INTERVAL_MINUTES` (default 15) the poller asks Linear for every issue updated since its last position, oldest first, and follows Linear's page cursors 50 issues at a time until `pageInfo` says there are no more. One poll reads at most 20 pages, 1,000 issues. Its position is the last issue it published, saved when the poll ends, after an error too; the next poll continues from there. The first poll looks back seven days.

## Sentry

Create an internal integration in Sentry under **Settings → Developer Settings**, then:

```bash
checkcord connect sentry
```

Enter the integration's client secret when asked. Paste the webhook URL the command prints into the integration and enable issue and error webhooks.

## Datadog

```bash
checkcord connect datadog
```

The command prints a webhook URL and a token. Create a webhook under **Integrations → Webhooks** with that URL and the custom header it shows, then mention the webhook in the monitors that should report to Checkcord.

## Zoom

Create a Zoom app with event subscriptions, then:

```bash
checkcord connect zoom --account-id <zoom account id>
```

Enter the app's secret token when asked. Paste the printed URL as the event notification endpoint, subscribe to the recording and transcript completed events, and press **Validate**.

Checkcord downloads each transcript with the event's `download_token`, splits it into topics, and asks `MEETING_EXTRACTION_LLM_MODEL` for the decisions, constraints and risks in each part. An extraction becomes a record only when its numbers and identifiers occur in that part of the transcript, enough of its words do, and the model then confirms the transcript supports it. A decision or constraint must also be judged settled by `CHAT_SETTLED_MODEL` against that part, not left open as a proposal or an unresolved objection. The transcript itself is not stored as an event: the webhook answers `202` with the ids of the transcript parts it queued, and each extraction that passes is stored as an event of its own. With `MEETING_JUDGE_MODE=direct` (the default) the check runs at once on any provider; with `batch` it waits for OpenAI's Batch API. See [configuration](configuration.md#models).

## Teams meetings

```bash
checkcord connect teams-meetings --ms-tenant-id <microsoft tenant id>
```

Checkcord creates the Microsoft Graph subscription on meeting transcripts for you, and keeps it alive:

- It uses the Teams app credentials from [Microsoft Teams](#microsoft-teams): `MSTEAMS_CLIENT_ID` and `MSTEAMS_CLIENT_SECRET`. Grant the app the `OnlineMeetingTranscript.Read.All` application permission, with admin consent, in that Microsoft tenant.
- The subscription is on `communications/onlineMeetings/getAllTranscripts`. Graph sends new transcripts to `PUBLIC_BASE_URL/api/v1/webhooks/msteams/meetings`, and subscription events to `.../meetings/lifecycle`. Both must be reachable from the internet over HTTPS before you connect; Graph checks them when the subscription is created.
- Each subscription lasts 70 hours. The API checks every hour and renews any that expire within a day. If Graph has dropped one, Checkcord creates a new one. When Graph sends `reauthorizationRequired` or `subscriptionRemoved`, Checkcord renews or recreates it at once.
- The subscription id, its expiry and the last error are stored with the connection. `checkcord doctor` shows a `teams subscription` line: `PASS` when every subscription is live, `WARN` with the error when a renewal failed, `FAIL` when one has expired or was never created.
- `checkcord disconnect teams-meetings <tenant id>` deletes the subscription in Graph. If that fails, the connection is still removed and the subscription lapses within 70 hours.

If the command cannot create the subscription, for example because the permission is missing, it prints the reason and keeps the connection. The hourly check tries again.

Transcripts are handled as for [Zoom](#zoom): the same extraction, checks and `MEETING_JUDGE_MODE`.

This has been tested against a stand-in for Graph only, not against a real Microsoft tenant. Check `checkcord doctor` and the first transcripts after you connect.

## Signed webhook

For a system Checkcord has no connector for, if it can send JSON and compute an HMAC:

```bash
checkcord connect webhook --name status-page
```

The command prints a signing secret, once, and a URL of the form `<PUBLIC_BASE_URL>/api/v1/webhooks/signed/<id>`. Each event is a `POST` of JSON to that URL with two headers:

- `X-Checkcord-Timestamp`: the current time in Unix seconds. Checkcord refuses a timestamp more than 300 seconds away from its clock.
- `X-Checkcord-Signature`: `sha256=` followed by the hex HMAC-SHA256 of `<timestamp>.<body>` under the secret, where `<body>` is the exact bytes sent.

```json
{
  "id": "incident-2291",
  "text": "Decision: card tokens are kept for 24 hours at most.",
  "title": "Checkout incident review",
  "url": "https://status.example.com/incidents/2291",
  "container": "payments",
  "author": {"id": "u-17", "name": "Status page", "email": "ops@example.com"},
  "audience": {"emails": ["dev@example.com"], "public": false}
}
```

- `id` and `text` are required. `id` is the event's id in the sending system.
- `container` is what [scope](#choosing-what-is-ingested) matches, such as a project or folder.
- `audience.emails` are matched to active Checkcord people, ignoring case. The answer counts those that matched nobody in `unknown_emails`. `audience.identity_ids` takes Checkcord user ids directly. With `"public": true` everyone in the organization can read it. With no audience the event is private to nobody, so only an admin grant opens it.

Checkcord answers `202` with `{"status": "accepted", "event_id": "…"}`, or `"status": "skipped"` when the scope excludes the container. A bad signature, an old timestamp or an unknown URL gets `401`. [`examples/signed_webhook.py`](../examples/signed_webhook.py) sends one event.

Run `checkcord connect webhook --name status-page --rotate-secret` to replace the secret. Each `--name` is its own connection, `webhook:status-page`, with its own review state and scope.

## Push API

Send anything else with an API key:

```bash
curl -X POST "$CHECKCORD_URL/api/v1/ingest/custom" \
  -H "Authorization: Bearer $CHECKCORD_KEY" -H "Content-Type: application/json" \
  -d '{
        "content_raw": "Decision: payment retries are capped at three attempts.",
        "source_id": "adr-0042",
        "happened_at": "2026-03-02T10:00:00Z",
        "access_control": {"is_public": true, "visibility": "public"}
      }'
```

Only `content_raw` is required. `access_control` says who may see the content. If you leave it out the event is private: it is stored and processed, but no person-scoped read returns it. Mark it public as above, or list the users who may see it in `allowed_identity_ids`.

Everything pushed shares one connection, `custom:push`. Put it in review with `checkcord connections review custom:push on`.

## Provider addresses

GitHub Enterprise Server and the Microsoft government and sovereign clouds serve the same APIs at other addresses. Point Checkcord at them with `GITHUB_API_URL`, `MSGRAPH_URL`, `MS_LOGIN_URL` and `BOTFRAMEWORK_OPENID_URL`; Slack, Linear and Notion have matching settings. The full list is in [configuration.md](configuration.md#provider-addresses). Checkcord has not been run against GitHub Enterprise Server or any government or sovereign cloud, so treat these as untested.

## Provider rate limits

When Slack, Microsoft Graph, GitHub, Jira, Notion or Linear answers `429 Too Many Requests`, Checkcord waits as long as the `Retry-After` header asks, up to 60 seconds, and tries again. Without the header it waits 2, 4 and 8 seconds. It gives up after four attempts, logs `provider_rate_limited.gave_up`, and the poll or refresh tries again at its next run. Each wait adds to `checkcord_provider_rate_limited_total`.

Two paths keep their own bounded retry and do not add to that metric: Slack audience and directory reads use the Slack client's retry, which honours `Retry-After` up to three times, and GitHub calls retry four times with their own waits.

## Who can read what

A record's audience is the audience of the event it was made from, its base event, plus any admin grants. Later events join it only if they do not widen who could read it:

- Any event may join a public record. A private follow-up on a public decision leaves the decision public. The follow-up's text, source line and link are shown only to people who may read the follow-up.
- A public event never joins a private record. A public restatement of a private decision becomes its own public record.
- A private event joins a private record only if everyone who may read the event may read the record.

The evidence score counts every linked event, whoever can read them. If the base event is purged, the earliest remaining event takes its place, and the record takes its text and audience.

| Connector | Audience of its events | Refreshed |
|---|---|---|
| Slack | Public channel: public. Private channel or direct message: members Checkcord can match | Yes: on membership and visibility events, and on schedule |
| Microsoft Teams | Standard channel: public. Private or shared channel, or chat: members Checkcord can match | Yes: on schedule |
| GitHub, including CI runs | Public repository: public. Private or internal repository: collaborators Checkcord can match | Yes: on `member` and `repository` webhooks, and on schedule |
| Zoom, Teams meetings | Attendees Checkcord can match | No |
| Jira, Notion, Linear | What an admin set for the connection or the project, team or parent; nobody until set | Yes: when the setting or a SCIM group changes, and on schedule |
| Sentry, Datadog | What an admin set for the connection; nobody until set | Yes: when the setting or a SCIM group changes, and on schedule |
| Agent logs | Private with an empty audience | No |
| Signed webhook | The people its `audience.emails` match, or public | No |
| Push API | What the sender puts in `access_control` | No |

Checkcord stores one audience for each Slack channel, Teams conversation and GitHub repository. New events from that channel or repository use it. It is read again from the provider every `AUDIENCE_REFRESH_INTERVAL_MINUTES` (default 60), and right away when Slack or GitHub reports a change. A change is applied to every event from that container and to every record built from them in one step.

- People Checkcord cannot match to one of its users are left out.
- A channel or repository that turns private is restricted at the first refresh that sees it.
- If the provider cannot be read, or the connection was removed, the last audience is kept for `AUDIENCE_REFRESH_GRACE_HOURS` (default 24). After that the audience is empty until a read succeeds.

Run `checkcord audiences list` to see each stored audience, how many of its members are matched to a person ("3 of 4 members matched"), and when it was last read. `checkcord audiences refresh` reads one now. Each refresh and membership change is written to the audit log as `audience.refreshed`, with counts only.

### Matching accounts to people

Slack and Teams accounts are matched by the email the platform gives, ignoring case. GitHub logins are matched by the public profile email. An account that matches nobody is left out of every restricted audience it is in. To match one by hand:

```bash
checkcord accounts list --unmatched
checkcord accounts link github octo-dev dev@example.com
checkcord accounts unlink slack U0123ABCD
checkcord accounts import links.csv
```

The CSV has the header `platform,account,email`. An account not seen yet is created, so a link can exist before the first sync. A sync never changes a link made by hand, and an unlinked account stays unlinked until it is linked again. The console's People page lists the accounts not matched to a person and links them. Each change is audited as `account.linked` or `account.unlinked`, and the audiences that list the account are refreshed at once. `checkcord doctor` warns while any account in a restricted channel or repository is unmatched.

### Audiences set by an admin

Jira, Notion, Linear, Sentry and Datadog say nothing about who may read what they send. An admin sets it for each connection, and optionally for one Jira project, Linear team or Notion parent (the same values the scope takes). The most specific setting wins.

```bash
checkcord connections audience jira:acme.atlassian.net --group engineering
checkcord connections audience jira:acme.atlassian.net --container PAY --group payments-team
checkcord connections audience linear:acme --people lead@example.com ops@example.com
checkcord connections audience sentry:acme --everyone
checkcord connections audience notion:acme --nobody
```

- `--nobody` is the default: records are stored but no person can read them.
- `--everyone` makes them public.
- `--group` takes SCIM group display names. Their active members can read.
- `--people` takes emails of active people.

New events take the setting when they arrive. A change is applied to what was already stored, and to the records made from it, in batches; `checkcord connections` shows how far it got. A SCIM group change, or a person deactivated or deleted, updates the settings that name them. The scheduled audience refresh resolves groups again. Changes are audited as `connection.audience_changed` with the setting before and after. `checkcord doctor` warns for each such connection whose records are readable by nobody. The admin API has `GET` and `PUT /api/v1/admin/connections/audience`, and the console's Connections page shows and changes it.

## Users

Checkcord decides who can see a record by matching people across systems, so it needs your user directory. Point your identity provider's SCIM 2.0 provisioning at `<PUBLIC_BASE_URL>/scim/v2` with an admin key as the bearer token; [provisioning.md](provisioning.md) has Okta and Entra ID steps. Without SCIM, create users as you issue keys; see [agent-setup.md](agent-setup.md).

"""Control API: scenarios change the world and read back what the stand-ins received."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiohttp import web

from sim import company

if TYPE_CHECKING:
    from sim.world import World


def _channel_id(name: str) -> str:
    return company.channel(name).id if name in company.CHANNEL_BY_NAME else name


def _user_id(key: str | None) -> str | None:
    if key is None:
        return None
    return company.person(key).slack_id if key in company.BY_KEY else key


def routes(app: web.Application, world: World) -> None:
    slack, github, jira, model, idp = world.slack, world.github, world.jira, world.model, world.idp
    zoom, notion, linear, teams = world.zoom, world.notion, world.linear, world.teams

    async def body(request: web.Request) -> dict[str, Any]:
        data: dict[str, Any] = await request.json() if request.body_exists else {}
        return data

    async def health(_: web.Request) -> web.Response:
        return web.json_response(
            {"ok": True, "slack_connected": slack.connected, "slack_installed": slack.installed}
        )

    async def slack_post(request: web.Request) -> web.Response:
        b = await body(request)
        msg = await slack.post(
            _channel_id(b["channel"]),
            _user_id(b.get("person")),
            b.get("text", ""),
            at=b.get("at"),
            thread_ts=b.get("thread_ts"),
            subtype=b.get("subtype"),
            bot_id=b.get("bot_id"),
            username=b.get("username"),
            file_text=b.get("file_text"),
            deliver=b.get("deliver", True),
        )
        return web.json_response({"ts": msg.ts, "channel": msg.channel, "subtype": msg.subtype})

    async def slack_edit(request: web.Request) -> web.Response:
        b = await body(request)
        await slack.edit(_channel_id(b["channel"]), b["ts"], b["text"], b.get("at"))
        return web.json_response({"ok": True})

    async def slack_delete(request: web.Request) -> web.Response:
        b = await body(request)
        await slack.delete(_channel_id(b["channel"]), b["ts"], b.get("at"))
        return web.json_response({"ok": True})

    async def slack_membership(request: web.Request) -> web.Response:
        b = await body(request)
        user = _user_id(b["person"])
        assert user is not None
        await slack.membership(_channel_id(b["channel"]), user, bool(b["joined"]))
        return web.json_response({"ok": True})

    async def slack_convert(request: web.Request) -> web.Response:
        b = await body(request)
        await slack.convert_to_private(_channel_id(b["channel"]), _user_id(b["person"]) or "")
        return web.json_response({"ok": True})

    async def slack_channel(request: web.Request) -> web.Response:
        b = await body(request)
        members = [str(_user_id(m)) for m in b["members"]]
        await slack.create_channel(b["id"], b["name"], bool(b.get("private", True)), members)
        return web.json_response({"ok": True, "id": b["id"]})

    async def slack_send_existing(request: web.Request) -> web.Response:
        b = await body(request)
        await slack.send_existing(_channel_id(b["channel"]), b["ts"])
        return web.json_response({"ok": True})

    async def slack_redeliver(request: web.Request) -> web.Response:
        return web.json_response(await slack.redeliver_event((await body(request))["ts"]))

    async def slack_uninstall(_: web.Request) -> web.Response:
        await slack.uninstall()
        return web.json_response({"ok": True})

    async def slack_fault(request: web.Request) -> web.Response:
        slack.rate_limit_next = int((await body(request)).get("count", 0))
        return web.json_response({"ok": True})

    async def slack_state(_: web.Request) -> web.Response:
        return web.json_response(slack.state())

    async def slack_event(request: web.Request) -> web.Response:
        envelope = await slack.deliver(await body(request))
        return web.json_response({"envelope_id": envelope["envelope_id"]})

    async def github_register(request: web.Request) -> web.Response:
        b = await body(request)
        repo = github.repos[b["repo"]]
        repo.webhook_url, repo.webhook_secret = b["url"], b["secret"]
        return web.json_response({"ok": True})

    async def github_pull(request: web.Request) -> web.Response:
        b = await body(request)
        author = company.person(b["author"]).github_login
        result = await github.open_pull(
            b["repo"], author, b["title"], b.get("body", ""), b["changes"]
        )
        return web.json_response(result)

    async def github_sync(request: web.Request) -> web.Response:
        b = await body(request)
        return web.json_response(await github.synchronize(b["repo"], b["number"], b.get("changes")))

    async def github_close(request: web.Request) -> web.Response:
        b = await body(request)
        return web.json_response(await github.close(b["repo"], b["number"], b.get("merge", True)))

    async def github_webhook(request: web.Request) -> web.Response:
        b = await body(request)
        return web.json_response(
            await github.send(b["repo"], b["event"], b["payload"], bool(b.get("forge")))
        )

    async def github_received(request: web.Request) -> web.Response:
        return web.json_response(github.received(request.query["repo"]))

    async def github_file(request: web.Request) -> web.Response:
        q = request.query
        return web.json_response({"text": github.file_at(q["repo"], q["ref"], q["path"])})

    async def jira_register(request: web.Request) -> web.Response:
        b = await body(request)
        if b.get("also"):
            jira.extra_hooks.append((b["url"], b["secret"]))
            if b.get("project") and b.get("host"):
                jira.project_hosts[b["project"]] = b["host"]
        else:
            jira.webhook_url, jira.webhook_secret = b["url"], b["secret"]
        return web.json_response({"ok": True})

    async def jira_issue(request: web.Request) -> web.Response:
        b = await body(request)
        return web.json_response(
            await jira.create(
                b["key"],
                b["summary"],
                b.get("description", ""),
                b["reporter"],
                b.get("issuetype", "Task"),
                b.get("at"),
            )
        )

    async def jira_update(request: web.Request) -> web.Response:
        b = await body(request)
        return web.json_response(
            await jira.update(b["key"], b["by"], b.get("summary"), b.get("description"))
        )

    async def jira_comment(request: web.Request) -> web.Response:
        b = await body(request)
        return web.json_response(await jira.comment(b["key"], b["by"], b["text"]))

    async def jira_redeliver(_: web.Request) -> web.Response:
        return web.json_response(await jira.redeliver())

    async def zoom_register(request: web.Request) -> web.Response:
        b = await body(request)
        zoom.webhook_url, zoom.secret = b["url"], b["secret"]
        return web.json_response(await zoom.validate())

    async def zoom_meeting(request: web.Request) -> web.Response:
        b = await body(request)
        lines = [(str(s), str(t)) for s, t in b["lines"]]
        return web.json_response(
            await zoom.meeting(b["topic"], b["host_email"], lines, int(b["minutes"]))
        )

    async def zoom_downloads(_: web.Request) -> web.Response:
        return web.json_response({"downloads": zoom.downloads})

    async def teams_message(request: web.Request) -> web.Response:
        b = await body(request)
        return web.json_response(
            await teams.message(
                b["channel"], b["by"], b["text"], b.get("reply_to"), b.get("auth", "good")
            )
        )

    async def teams_transcript(request: web.Request) -> web.Response:
        b = await body(request)
        lines = [(str(s), str(t)) for s, t in b["lines"]]
        return web.json_response(
            await teams.transcript(b["organizer"], b["subject"], lines, int(b["minutes"]))
        )

    async def teams_state(_: web.Request) -> web.Response:
        return web.json_response(teams.state())

    async def notion_page(request: web.Request) -> web.Response:
        b = await body(request)
        page = notion.add_page(b["title"], b["paragraphs"], b.get("parent", "root"), b["by"])
        return web.json_response({"id": page["id"], "url": page["url"]})

    async def linear_issue(request: web.Request) -> web.Response:
        b = await body(request)
        made = []
        for item in b["issues"]:
            comments = [(str(n), str(t)) for n, t in item.get("comments", [])]
            issue = linear.add_issue(
                item["team"], item["title"], item.get("description", ""), comments, item.get("at")
            )
            made.append({"id": issue["id"], "identifier": issue["identifier"]})
        return web.json_response({"issues": made})

    async def linear_read(_: web.Request) -> web.Response:
        return web.json_response({"queries": linear.queries, "issues": len(linear.issues)})

    async def model_mode(request: web.Request) -> web.Response:
        try:
            model.set_mode(str((await body(request))["mode"]))
        except ValueError as err:
            return web.json_response({"error": str(err)}, status=400)
        return web.json_response(model.summary())

    async def model_read(request: web.Request) -> web.Response:
        since = int(request.query.get("since", "0"))
        return web.json_response({**model.summary(), "log": model.state.bodies[since:]})

    async def idp_groups(request: web.Request) -> web.Response:
        b = await body(request)
        idp.groups_by_email = {k.lower(): list(v) for k, v in b.items()}
        return web.json_response({"ok": True})

    async def requests_read(request: web.Request) -> web.Response:
        since = int(request.query.get("since", "0"))
        rows = world.log.since(since, request.query.get("service"))
        return web.json_response({"last": world.log.last, "items": [r.as_dict() for r in rows]})

    async def deliveries_read(request: web.Request) -> web.Response:
        since = int(request.query.get("since", "0"))
        items = [d for d in world.deliveries.items if d["seq"] > since]
        return web.json_response({"last": len(world.deliveries.items), "items": items})

    r = app.router
    r.add_get("/control/health", health)
    r.add_post("/control/slack/post", slack_post)
    r.add_post("/control/slack/edit", slack_edit)
    r.add_post("/control/slack/delete", slack_delete)
    r.add_post("/control/slack/membership", slack_membership)
    r.add_post("/control/slack/convert", slack_convert)
    r.add_post("/control/slack/channel", slack_channel)
    r.add_post("/control/slack/send", slack_send_existing)
    r.add_post("/control/slack/redeliver", slack_redeliver)
    r.add_post("/control/slack/uninstall", slack_uninstall)
    r.add_post("/control/slack/fault", slack_fault)
    r.add_post("/control/slack/event", slack_event)
    r.add_get("/control/slack/state", slack_state)
    r.add_post("/control/github/register", github_register)
    r.add_post("/control/github/pull", github_pull)
    r.add_post("/control/github/synchronize", github_sync)
    r.add_post("/control/github/close", github_close)
    r.add_post("/control/github/webhook", github_webhook)
    r.add_get("/control/github/received", github_received)
    r.add_get("/control/github/file", github_file)
    r.add_post("/control/jira/register", jira_register)
    r.add_post("/control/jira/issue", jira_issue)
    r.add_post("/control/jira/update", jira_update)
    r.add_post("/control/jira/comment", jira_comment)
    r.add_post("/control/jira/redeliver", jira_redeliver)
    r.add_post("/control/zoom/register", zoom_register)
    r.add_post("/control/zoom/meeting", zoom_meeting)
    r.add_get("/control/zoom/downloads", zoom_downloads)
    r.add_post("/control/teams/message", teams_message)
    r.add_post("/control/teams/transcript", teams_transcript)
    r.add_get("/control/teams/state", teams_state)
    r.add_post("/control/notion/page", notion_page)
    r.add_post("/control/linear/issues", linear_issue)
    r.add_get("/control/linear", linear_read)
    r.add_post("/control/model/mode", model_mode)
    r.add_get("/control/model", model_read)
    r.add_post("/control/idp/groups", idp_groups)
    r.add_get("/control/requests", requests_read)
    r.add_get("/control/deliveries", deliveries_read)

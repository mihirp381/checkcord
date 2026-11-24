"""Easy level: clear cases, each with its own subject."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from datetime import UTC, datetime
from typing import Any

from sim import company
from sim.client import API, ENTRA_DEACTIVATE, ROOT, Reply, compose
from sim.harness import (
    SLACK_CONNECTION,
    Ctx,
    all_record_ids,
    aws_style_key,
    cli_counts,
    connect_github,
    doctor_lines,
    hit,
    norm,
    secret_after,
    signed_headers,
    snippet,
    url_after,
    webhook_test_secret,
)
from sim.harness import scenario as register

PAY = company.PAYMENTS
EVERYONE = tuple(p.key for p in company.PEOPLE)
PRIVATE_READERS = ("tomas", "aiko", "ingrid")


def scenario(sid: str, title: str, last: bool = False) -> Any:
    return register(sid, title, "easy", last)


# Shared steps


async def record_texts(ctx: Ctx, ids: list[str]) -> dict[str, str]:
    out = {}
    for rid in ids:
        r = await ctx.admin.record(rid)
        out[rid] = r.body.get("summary", "") if r.ok else ""
    return out


async def one_record(ctx: Ctx, *phrases: str, timeout: float = 120) -> list[str]:
    async def found() -> list[str]:
        ids: list[str] = []
        for p in phrases:
            ids += [i for i in await ctx.records_from(p) if i not in ids]
        return ids

    ids, _ = await ctx.wait_for(found, timeout, every=3)
    return list(ids or [])


async def event_ids(ctx: Ctx, *phrases: str) -> list[str]:
    ids: list[str] = []
    for p in phrases:
        ids += [e["event_id"] for e in await ctx.events_with(p) if e["event_id"] not in ids]
    return ids


def remember(ctx: Ctx, name: str, rid: str | None, text: str | None, private: bool = False) -> None:
    ctx.shared.setdefault("records", {})[name] = {"id": rid, "text": text}
    if private and rid:
        ctx.shared.setdefault("restricted", {})[name] = {"id": rid, "text": text}


def known(ctx: Ctx, name: str) -> tuple[str | None, str | None]:
    r = ctx.shared.get("records", {}).get(name) or {}
    return r.get("id"), r.get("text")


async def read_routes(
    ctx: Ctx,
    key: str,
    rid: str | None,
    text: str | None,
    *,
    query: str,
    repo: str = PAY,
    symbols: tuple[str, ...] = (),
    file_path: str | None = None,
) -> dict[str, Any]:
    """Every route that can return record text, as one person; True where the record came back."""
    a = ctx.person(key)
    out: dict[str, Any] = {}
    r = await a.search(query, repo, limit=50)
    out["mcp search_conceptual_context"] = hit(r.body, rid, text)
    r = await a.active_decisions(repo)
    out["mcp view_active_decisions"] = hit(r.body, rid, text)
    r = await a.active_decisions()
    out["mcp view_active_decisions (no repo)"] = hit(r.body, rid, text)
    if file_path:
        r = await a.dependency_flow(file_path)
        out["mcp get_dependency_flow"] = hit(r.body, rid, text)
    r = await a.graph("decisions/active", repo_identifier=repo)
    out["graph decisions/active"] = r.ok and hit(r.body, rid, text)
    for s in symbols:
        r = await a.graph("context", symbol=s)
        out[f"graph context {s}"] = r.ok and hit(r.body, rid, text)
        r = await a.graph("impact", symbol=s)
        out[f"graph impact {s}"] = r.ok and hit(r.body, rid, text)
    r = await a.graph("network", repo_identifier=repo)
    out["graph network"] = r.ok and hit(r.body, rid, text)
    if rid:
        r = await a.node(f"context:{rid}", repo)
        out["graph node"] = r.ok and hit(r.body, rid, text)
        r = await a.get(f"/api/v1/graph/context/{rid}/scope/history")
        out["graph scope history"] = r.ok and hit(r.body, rid, text)
        r = await a.get(f"/api/v1/admin/records/{rid}")
        out["admin record with personal key"] = r.ok
        r = await a.get(f"/api/v1/admin/raw-content/context:{rid}", repo_identifier=repo)
        out["admin raw-content with personal key"] = r.ok
    return out


def any_true(routes: dict[str, Any]) -> list[str]:
    return [k for k, v in routes.items() if v is True]


async def can_read(ctx: Ctx, key: str, rid: str | None, text: str | None, query: str) -> bool:
    a = ctx.person(key)
    s = await a.search(query, PAY, limit=50)
    d = await a.active_decisions(PAY)
    return hit(s.body, rid, text) or hit(d.body, rid, text)


async def admin_action(ctx: Ctx, name: str, console: tuple[str, str, Any], cli: list[str]) -> Any:
    """Run one admin action through Ingrid's console session or the CLI, alternating."""
    via = ctx.via()
    if via == "console":
        method, path, body = console
        c = await ctx.console("ingrid")
        r = await c.call(method, path, json_body=body)
        result: Any = {"via": "console", "status": r.status, "body": r.body}
        ok = r.ok
    else:
        res = await ctx.cli(*cli)
        result = {"via": "cli", "exit": res.code, "output": res.text[-3000:]}
        ok = res.code == 0
    ctx.observe(f"admin action: {name}", result)
    result["ok"] = ok
    return result


async def wait_status(ctx: Ctx, repo: str, sha: str, timeout: float = 240) -> dict[str, Any] | None:
    async def final() -> dict[str, Any] | None:
        got = await ctx.world.get("/github/received", repo=repo)
        rows = [s for s in got["statuses"] if s["sha"] == sha]
        done = [s for s in rows if s["state"] != "pending"]
        return done[-1] if done else None

    found, _ = await ctx.wait_for(final, timeout, every=3)
    return dict(found) if found else None


async def comments_on(ctx: Ctx, repo: str, number: int) -> list[str]:
    got = await ctx.world.get("/github/received", repo=repo)
    return [c["body"] for c in got["comments"] if c["issue"] == number]


async def open_pull(
    ctx: Ctx, repo: str, author: str, title: str, changes: dict[str, str | None]
) -> dict[str, Any]:
    pr: dict[str, Any] = await ctx.world.call(
        "/github/pull", repo=repo, author=author, title=title, body="", changes=changes
    )
    ctx.sent.append({"github": "pull_request opened", "repo": repo, "number": pr["number"]})
    return pr


def sensitive(ctx: Ctx) -> dict[str, str]:
    return dict(ctx.shared.get("sensitive", {}))


def leaks(body: Any, strings: dict[str, str]) -> list[str]:
    raw = body if isinstance(body, str) else json.dumps(body, default=str, ensure_ascii=False)
    flat = raw.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    found = []
    for name, value in strings.items():
        bare = value.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if value in raw or bare in flat:
            found.append(name)
    return found


def restricted_in(ctx: Ctx, body: Any) -> list[str]:
    return [n for n, r in ctx.shared.get("restricted", {}).items() if hit(body, r["id"], r["text"])]


async def audit_since(ctx: Ctx, since: str, **filters: Any) -> list[dict[str, Any]]:
    return await ctx.admin.audit(since=since, **filters)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_time(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


# Scenarios


@scenario("E01", "A public decision reaches every developer")
async def e01(ctx: Ctx) -> None:
    """In #eng, Tomas states the Idempotency-Key decision; Aiko and Omar reply in the thread.

    must: exactly one record comes from the thread; all nine people's keys get it from
    search_conceptual_context ("idempotency key", payments-api) and view_active_decisions;
    each read has an audit entry naming the person, key and record id.
    observe: the record text and type; seconds from the last message to the record.
    """
    root = await ctx.post(
        "eng",
        "tomas",
        "Decision: every POST endpoint in payments-api takes an Idempotency-Key header. "
        "create_charge rejects a request without one.",
    )
    await ctx.post("eng", "aiko", "Agreed, I'll add it to the client.", thread_ts=root)
    await ctx.post("eng", "omar", "+1, I'll update the API reference for it.", thread_ts=root)
    last = time.time()
    await ctx.settle()
    ids = await one_record(ctx, "Idempotency-Key header", "add it to the client")
    ctx.must("exactly one record comes from the thread", len(ids) == 1, {"records": ids})
    rid = ids[0] if ids else None
    text = (await record_texts(ctx, [rid])).get(rid) if rid else None
    remember(ctx, "E01", rid, text)
    if rid:
        detail = (await ctx.admin.record(rid)).body
        ctx.observe("record text and type", {"text": text, "type": detail.get("context_type")})
        ctx.observe(
            "seconds from last message to record",
            round(parse_time(detail["created_at"]) - last, 1),
        )
    read_start = now_iso()
    got: dict[str, dict[str, bool]] = {}
    for key in EVERYONE:
        a = ctx.person(key)
        s = await a.search("idempotency key", PAY)
        d = await a.active_decisions(PAY)
        got[key] = {"search": hit(s.body, rid, text), "active_decisions": hit(d.body, rid, text)}
    missing = {k: v for k, v in got.items() if not all(v.values())}
    ctx.must("all nine people's keys get it from search and active decisions", not missing, got)
    entries = await audit_since(ctx, read_start)
    reads = [
        e
        for e in entries
        if e["action"] in ("mcp.search_conceptual_context", "mcp.view_active_decisions")
    ]
    unnamed = []
    for key in EVERYONE:
        email = company.person(key).email
        mine = [e for e in reads if (e.get("user_email") or "").lower() == email]
        named = [
            e
            for e in mine
            if e.get("key_name") == f"sim-{key}" and rid in (e.get("record_ids") or [])
        ]
        expected = sum(got[key].values())
        if len(named) < expected:
            unnamed.append({"person": email, "reads_returning_it": expected, "entries": mine})
    ctx.must(
        "each read has an audit entry naming the person, key and record id", not unnamed, unnamed
    )


@scenario("E02", "A private channel stays private")
async def e02(ctx: Ctx) -> None:
    """In #payments-private, Tomas states the chargeback evidence retention decision.

    must: Tomas, Aiko and Ingrid can read it; Dev, Rosa, Omar, Lena, Kofi and Priya cannot, by
    search, view_active_decisions or the graph endpoints by id; the Audiences page shows the
    channel as private with 3 of 3 members matched. The context file and pull-request comments
    are checked in E06 and E17.
    """
    await ctx.post(
        "payments-private",
        "tomas",
        "Decision: chargeback evidence files stay in the vault bucket for 18 months and are "
        "deleted after. refund_charge must never copy them elsewhere.",
    )
    await ctx.settle()
    ids = await one_record(ctx, "chargeback evidence files")
    rid = ids[0] if ids else None
    text = (await record_texts(ctx, [rid])).get(rid) if rid else None
    ctx.must("a record is made from the message", bool(rid), {"records": ids})
    remember(ctx, "E02", rid, text, private=True)
    q = "chargeback evidence vault bucket retention"
    for key in PRIVATE_READERS:
        routes = await read_routes(
            ctx,
            key,
            rid,
            text,
            query=q,
            symbols=("refund_charge",),
            file_path="payments_api/charges.py",
        )
        reads = [k for k in any_true(routes) if k.startswith(("mcp", "graph decisions"))]
        ctx.must(f"{key} can read it", bool(reads), routes)
    for key in ("dev", "rosa", "omar", "lena", "kofi", "priya"):
        routes = await read_routes(
            ctx,
            key,
            rid,
            text,
            query=q,
            symbols=("refund_charge",),
            file_path="payments_api/charges.py",
        )
        ctx.must(f"{key} cannot read it by any route", not any_true(routes), routes)
        h = await ctx.person(key).get(f"/api/v1/graph/context/{rid}/scope/history")
        ctx.observe(f"{key}: scope history for the record id", {"status": h.status, "body": h.body})
    c = await ctx.console("ingrid")

    async def stored() -> list[dict[str, Any]]:
        page = await c.get("/api/v1/admin/audiences")
        return [
            r
            for r in (page.body.get("items", []) if page.ok else [])
            if company.channel("payments-private").id in r.get("container", "")
        ]

    rows, waited = await ctx.wait_for(stored, 90, every=5)
    ctx.observe("seconds until the audience was stored", waited)
    ctx.observe("audiences page rows for the channel", rows)
    ok = (
        len(rows) == 1
        and rows[0]["is_public"] is False
        and rows[0]["member_count"] == 3
        and rows[0].get("unmatched_count") == 0
    )
    ctx.must("Audiences page shows the channel private with 3 of 3 members matched", ok, rows)


@scenario("E03", "Obvious sensitive data is redacted everywhere")
async def e03(ctx: Ctx) -> None:
    """In #eng, Omar posts an escalation decision with an email, a phone number and two secrets.

    must: no read path returns the email, phone number or either secret (MCP tools, graph
    endpoints, node details, the console record page, `checkcord records show`; the context file
    and pull-request comments are checked in E06 and E17); raw-content returns the original in
    its sources and is audited; the audit entry of an agent read counts the record as redacted.
    """
    key = aws_style_key()
    secret = webhook_test_secret()
    ctx.run.secrets.add(key, secret)
    strings = {
        "email": "oncall-payments@marrowfield.example",
        "phone": "+1 415 555 0142",
        "aws key": key,
        "webhook secret": secret,
    }
    ctx.shared["sensitive"] = strings
    await ctx.post(
        "eng",
        "omar",
        "Decision: the on-call escalation for payments-api goes to "
        f"{strings['email']} or {strings['phone']}. The staging deploy key is {key} and the "
        f"webhook test secret is {secret}. Rotate both every quarter.",
    )
    await ctx.settle()
    ids = await one_record(ctx, "on-call escalation for payments-api")
    rid = ids[0] if ids else None
    ctx.must("a record is made from the message", bool(rid), {"records": ids})
    text = (await record_texts(ctx, [rid])).get(rid) if rid else None
    remember(ctx, "E03", rid, text)
    read_start = now_iso()
    found: dict[str, list[str]] = {}
    for who in ("omar", "dev", "ingrid"):
        a = ctx.person(who)
        bodies: dict[str, Reply] = {
            "mcp search": await a.search("on-call escalation rotate quarter", PAY),
            "mcp active decisions": await a.active_decisions(PAY),
            "mcp active decisions (no repo)": await a.active_decisions(),
            "mcp dependency flow": await a.dependency_flow("payments_api/charges.py"),
            "graph decisions/active": await a.graph("decisions/active", repo_identifier=PAY),
            "graph context": await a.graph("context", symbol="create_charge"),
            "graph impact": await a.graph("impact", symbol="create_charge"),
            "graph network": await a.graph("network", repo_identifier=PAY),
        }
        if rid:
            bodies["graph node"] = await a.node(f"context:{rid}", PAY)
        for route, r in bodies.items():
            bad = leaks(r.body, strings)
            if bad:
                found[f"{who} {route}"] = bad
    c = await ctx.console("ingrid")
    if rid:
        page = await c.get(f"/api/v1/admin/records/{rid}")
        if leaks(page.body, strings):
            found["console record page"] = leaks(page.body, strings)
        listing = await c.get("/api/v1/admin/records", q="escalation")
        if leaks(listing.body, strings):
            found["console records list"] = leaks(listing.body, strings)
        shown = await ctx.cli("records", "show", rid)
        if leaks(shown.out, strings):
            found["checkcord records show"] = leaks(shown.out, strings)
        ctx.observe("checkcord records show", shown.text)
    ctx.must("no read path returns the email, phone number or either secret", not found, found)
    if rid:
        raw_start = now_iso()
        raw = await ctx.admin.get(f"/api/v1/admin/raw-content/context:{rid}", repo_identifier=PAY)
        body = raw.body if raw.ok and isinstance(raw.body, dict) else {}
        sources = json.dumps(body.get("sources") or [], ensure_ascii=False)
        absent = [name for name, value in strings.items() if value not in sources]
        ctx.must(
            "raw-content returns the original",
            raw.ok and not absent,
            {"status": raw.status, "absent": absent},
        )
        entries = await audit_since(ctx, raw_start, action="admin.raw_content")
        ctx.must(
            "raw-content writes an audit entry",
            any(rid in json.dumps(e) for e in entries),
            entries,
        )
    entries = await audit_since(ctx, read_start)
    agent = [
        e for e in entries if e["action"].startswith("mcp.") and rid in (e.get("record_ids") or [])
    ]
    counted = [e for e in agent if (e.get("redacted_count") or 0) >= 1]
    ctx.must(
        "the audit entry of an agent read counts the record as redacted",
        bool(agent) and len(counted) == len(agent),
        agent,
    )


@scenario("E04", "Chatter is ignored")
async def e04(ctx: Ctx) -> None:
    """Small talk in #random and #eng, a bot reminder and a channel join.

    must: no record is created from any of them.
    observe: how each was classified and at which stage it stopped.
    """
    texts = [
        ("random", "dev", "anyone want lunch?"),
        ("random", "omar", ":taco:"),
        ("random", "dev", "lol"),
        ("random", "omar", "the coffee machine is broken again"),
        ("eng", "rosa", "thanks!"),
        ("eng", "dev", "can someone review my PR?"),
    ]
    sent: dict[str, str] = {}
    for channel, who, text in texts:
        sent[text] = await ctx.post(channel, who, text, chatter=True)
    sent["Standup reminder"] = await ctx.post(
        "random",
        None,
        "Standup reminder",
        subtype="bot_message",
        bot_id="B0STANDUP1",
        username="Standup bot",
    )
    await ctx.world.call("/slack/membership", channel="random", person="rosa", joined=True)
    ctx.sent.append({"slack": "random", "membership": "rosa joined"})
    await ctx.settle()
    made: dict[str, list[str]] = {}
    stages: dict[str, Any] = {}
    random_id = company.channel("random").id
    for text, ts in [*sent.items(), ("has joined", "")]:
        q = text.replace("'", "''")
        found = (
            f"metadata->'chat_source'->'message_ids' ? '{ts}'"
            if ts
            else f"audience_container = '{random_id}' AND content_raw ILIKE '%{q}%'"
        )
        rows = await ctx.run_sql(
            "SELECT event_id, signal_status, pipeline_state, coalesce(pipeline_last_error, '') "
            f"FROM events WHERE {found}"
        )
        stages[text] = [
            dict(zip(("event_id", "signal_status", "pipeline_state", "error"), r, strict=False))
            for r in rows
        ]
        ids: list[str] = []
        for r in rows:
            linked = await ctx.run_sql(
                f"SELECT canonical_id FROM context_event_links WHERE event_id = '{r[0]}' UNION "
                f"SELECT id FROM canonical_contexts WHERE base_event_id = '{r[0]}'"
            )
            ids += [x[0] for x in linked]
        if ids:
            made[text] = ids
            ctx.track(*ids)
    ctx.observe("classification and stage per message", stages)
    ctx.must("no record is created from any of them", not made, made)


@scenario("E05", "A repository is connected and decisions attach to code")
async def e05(ctx: Ctx) -> None:
    """Connect marrowfield/payments-api, then Aiko states that quote_total returns integer cents.

    must: the code graph contains the repository's files and functions; the record is linked to
    quote_total; get_dependency_flow for charges.py returns it under quote_total for a
    collaborator; Dev, who is not a collaborator, gets neither the file's symbols nor the record.
    observe: time to index; what the graph links to; what Dev's dependency flow returns.
    """
    started = time.time()
    out = await connect_github(ctx.admin, ctx.world, ctx.run.state, PAY)
    ctx.run.secrets.add(ctx.run.state["webhook_secrets"][PAY])
    ctx.observe("connect output", out["output"])
    files = [p for p in company.PAYMENTS_FILES if p.endswith(".py") and company.PAYMENTS_FILES[p]]
    functions = [
        "quote_total",
        "create_charge",
        "refund_charge",
        "verify_signature",
        "handle_webhook",
        "retry_delivery",
        "post_entry",
        "LedgerClient",
        "mask_card",
    ]
    aiko = ctx.person("aiko")

    async def indexed() -> Any:
        r = await aiko.graph("network", repo_identifier=PAY)
        body = r.text if r.ok else ""
        return body if all(f in body for f in files) and all(f in body for f in functions) else None

    graph, waited = await ctx.wait_for(indexed, 600, every=5)
    ctx.observe("seconds to index", round(time.time() - started, 1))
    r = await aiko.graph("network", repo_identifier=PAY)
    missing = [f for f in files + functions if f not in r.text]
    ctx.must("the code graph contains the repository's files and functions", not missing, missing)
    await ctx.settle()
    await ctx.post(
        "eng",
        "aiko",
        "Decision: quote_total returns integer cents. Never use floats for money anywhere in "
        "payments-api.",
    )
    await ctx.settle()
    ids = await one_record(ctx, "quote_total returns integer cents")
    rid = ids[0] if ids else None
    ctx.must("a record is made from the message", bool(rid), {"records": ids})
    text = (await record_texts(ctx, [rid])).get(rid) if rid else None
    remember(ctx, "E05", rid, text)

    async def linked() -> bool:
        c = await aiko.graph("context", symbol="quote_total")
        return c.ok and hit(c.body, rid, text)

    ok, _ = await ctx.wait_for(linked, 120, every=5)
    c = await aiko.graph("context", symbol="quote_total")
    ctx.must("the record is linked to quote_total", bool(ok), c.body)
    flow = await aiko.dependency_flow("payments_api/charges.py")
    nodes = flow.body.get("nodes", []) if isinstance(flow.body, dict) else []
    under = [n for n in nodes if n.get("symbol_name") == "quote_total"]
    ctx.must(
        "get_dependency_flow for charges.py returns it under quote_total for a collaborator",
        bool(rid) and any(r.get("id") == rid for n in under for r in n.get("records", [])),
        flow.body,
    )
    other = await ctx.person("dev").dependency_flow("payments_api/charges.py")
    ctx.observe("Dev's dependency flow for charges.py", other.body)
    ctx.must(
        "Dev, not a collaborator, gets neither the file's symbols nor the record",
        isinstance(other.body, dict)
        and not other.body.get("nodes")
        and not (rid and rid in json.dumps(other.body)),
        other.body,
    )
    net = await aiko.graph("network", repo_identifier=PAY)
    if net.ok and rid:
        node = f"context:{rid}"
        edges = [e for e in net.body.get("edges", []) if node in (e.get("source"), e.get("target"))]
        ctx.observe("what the graph links the record to", edges)


@scenario("E06", "A pull request that breaks a blocking decision is stopped")
async def e06(ctx: Ctx) -> None:
    """The E05 record is made blocking; PR A makes quote_total a float, PR B renames a local.

    must: A fails with a comment citing the record (redacted, no restricted records); B passes
    with no comment; with the record advisory a new push on A passes and the comment shows it as
    advisory; stats count one blocked check; all of it is in the audit log. Also checks that no
    comment carries the E02 record or the E03 strings.
    observe: the comment text and the model's reason; model calls per check; seconds per check.
    """
    start = now_iso()
    stats_before = await ctx.admin.stats()
    usage_before = await ctx.admin.usage()
    delivered_from = (await ctx.world.get("/deliveries"))["last"]
    rid, text = known(ctx, "E05")
    if not rid:
        ctx.must("the E05 record exists", False, "E05 made no record")
        return
    await admin_action(
        ctx,
        "mark blocking",
        ("PATCH", f"/api/v1/admin/records/{rid}/severity", {"severity": "blocking"}),
        ["records", "severity", rid, "blocking"],
    )
    charges = company.PAYMENTS_FILES["payments_api/charges.py"]
    old = (
        "def quote_total(subtotal_cents: int) -> int:\n"
        '    """Total in integer cents, tax included."""\n'
        '    tax = (Decimal(subtotal_cents) * TAX_RATE).quantize(Decimal("1"))\n'
        "    return subtotal_cents + int(tax)\n"
    )
    floaty = "def quote_total(subtotal):\n    return round(subtotal * 1.0825, 2)\n"
    ledger = company.PAYMENTS_FILES["payments_api/ledger.py"]

    async def timed(sha: str) -> tuple[dict[str, Any] | None, float]:
        t0 = time.time()
        return await wait_status(ctx, PAY, sha), round(time.time() - t0, 1)

    a = await open_pull(
        ctx,
        PAY,
        "tomas",
        "Simplify quote_total",
        {"payments_api/charges.py": charges.replace(old, floaty)},
    )
    b = await open_pull(
        ctx,
        PAY,
        "aiko",
        "Rename a local in post_entry",
        {"payments_api/ledger.py": ledger.replace("pending_row", "row")},
    )
    (sa, ta), (sb, tb) = await asyncio.gather(timed(a["head_sha"]), timed(b["head_sha"]))
    ctx.observe("statuses", {"A": sa, "B": sb})
    ctx.must("A gets a failing status", (sa or {}).get("state") == "failure", sa)
    await asyncio.sleep(3)
    comments = await comments_on(ctx, PAY, a["number"])
    ctx.observe("comments on A", comments)
    reasons = [line for c in comments for line in c.splitlines() if "Reason:" in line]
    ctx.observe("the model's reason on A", reasons)
    ctx.must(
        "A gets a comment that cites the record", any(hit(c, rid, text) for c in comments), comments
    )
    bad = [n for c in comments for n in restricted_in(ctx, c)]
    ctx.must("the comment carries no restricted record", not bad, bad)
    raw_text = await ctx.admin.get(f"/api/v1/admin/raw-content/context:{rid}", repo_identifier=PAY)
    original = (
        raw_text.body.get("summary") if raw_text.ok and isinstance(raw_text.body, dict) else None
    )
    if original and original != text:
        ctx.must(
            "the comment cites the redacted text",
            not any(
                snippet(original, 12) in norm(c) and snippet(text, 12) not in norm(c)
                for c in comments
            ),
            comments,
        )
    ctx.must("B gets a passing status", (sb or {}).get("state") == "success", sb)
    on_b = await comments_on(ctx, PAY, b["number"])
    ctx.must("B gets no comment", not on_b, on_b)
    await admin_action(
        ctx,
        "mark advisory",
        ("PATCH", f"/api/v1/admin/records/{rid}/severity", {"severity": "advisory"}),
        ["records", "severity", rid, "advisory"],
    )
    rerun = await ctx.world.call(
        "/github/synchronize",
        repo=PAY,
        number=a["number"],
        changes={"payments_api/charges.py": charges.replace(old, floaty) + "\n"},
    )
    ctx.sent.append({"github": "pull_request synchronize", "repo": PAY, "number": a["number"]})
    sr, tr = await timed(rerun["head_sha"])
    ctx.observe("seconds per check", {"A": ta, "B": tb, "A after the push": tr})
    ctx.must(
        "with the record advisory, a new push on A is non-blocking",
        (sr or {}).get("state") == "success",
        sr,
    )
    await asyncio.sleep(3)
    after_push = await comments_on(ctx, PAY, a["number"])
    ctx.observe("comments on A after the push", after_push)
    ctx.must(
        "A keeps one comment, now showing the record as advisory",
        len(after_push) == 1
        and hit(after_push[0], rid, text)
        and "**Advisory**" in after_push[0]
        and rerun["head_sha"][:7] in after_push[0],
        after_push,
    )
    await ctx.settle()
    stats_after = await ctx.admin.stats()
    usage_after = await ctx.admin.usage()
    before, after = stats_before.get("pr_checks", {}), stats_after.get("pr_checks", {})
    keys = ("posted", "skipped", "flagged", "conflicts", "blocked", "errors")
    delta = {k: after.get(k, 0) - before.get(k, 0) for k in keys}

    def pr_calls(usage: dict[str, Any]) -> int:
        return sum(r["calls"] for r in usage.get("rows", []) if r.get("purpose") == "pr_check")

    delivered = [
        d
        for d in (await ctx.world.get("/deliveries", since=delivered_from))["items"]
        if d["service"] == "github"
        and d["action"] in ("opened", "synchronize")
        and 200 <= int(d.get("status") or 0) < 300
    ]
    others = [d for d in delivered if d.get("number") not in (a["number"], b["number"])]
    ctx.observe("stats pr_checks change", delta)
    ctx.observe(
        "other pull-request deliveries in the window",
        [{k: d.get(k) for k in ("repo", "number", "action")} for d in others],
    )
    ctx.must(
        "stats count every check in the window (A, B, the re-run and any other delivery, "
        "posted or skipped), one blocked",
        delta["posted"] + delta["skipped"] == len(delivered) and delta["blocked"] == 1,
        {"stats": delta, "deliveries": len(delivered), "other deliveries": len(others)},
    )
    entries = await audit_since(ctx, start, action="pr.check_posted")
    ours = [e for e in entries if (e.get("params") or {}).get("pr") in (a["number"], b["number"])]
    ctx.must(
        "the audit log has the three checks of A and B",
        len(ours) == 3,
        [e.get("params") for e in entries],
    )
    ctx.observe(
        "model calls per check",
        {
            "records judged, from the audit log": [
                {"pr": p.get("pr"), "judged": len(p.get("verdicts") or {})}
                for p in (e.get("params") or {} for e in ours)
            ],
            "pr_check calls in usage": pr_calls(usage_after) - pr_calls(usage_before),
        },
    )
    every = []
    for n in (a["number"], b["number"]):
        every += await comments_on(ctx, PAY, n)
    ctx.must(
        "no comment carries the E02 record", not [c for c in every if restricted_in(ctx, c)], every
    )
    if sensitive(ctx):
        found = [leaks(c, sensitive(ctx)) for c in every]
        ctx.must("no comment carries the E03 strings", not any(found), found)


async def jira_searches(ctx: Ctx) -> int:
    reqs = (await ctx.world.requests(service="jira")).get("items", [])
    return sum(1 for r in reqs if r["path"].endswith("/search"))


@scenario("E07", "A new connection is held for review")
async def e07(ctx: Ctx) -> None:
    """Connect Jira project PAY with review on; PAY-101 states a decision, PAY-102 an idea.

    A ticket is not extracted, so PAY-101's record is a reference record of type general that
    agents find by search, not in view_active_decisions.

    must: neither is readable by any agent while held; the Review page lists what is held;
    PAY-101's record is a reference record;
    after releasing PAY-101's record nobody can read it and the doctor warns, since Jira says
    nothing about who may read; after the admin sets project PAY's audience to the
    payments-team group, its three members can read it and the others cannot; rejecting
    PAY-102's deletes it and its events; the rejected ticket does not come back with the next
    poll; release, reject and the audience change are audited.
    """
    start = now_iso()
    host = f"http://host.docker.internal:{ctx.world.base.rsplit(':', 1)[1]}"
    res = await ctx.cli(
        "connect",
        "jira",
        "--host",
        host,
        "--project-key",
        "PAY",
        "--review",
        env={"CHECKCORD_JIRA_TOKEN": ctx.run.state["jira_token"]},
    )
    secret = secret_after("Webhook secret", res.out)
    url = url_after("URL:", res.out)
    ctx.observe("connect output", res.text)
    if res.code != 0 or not secret or not url:
        ctx.must("Jira connects", False, res.text[-1000:])
        return
    ctx.run.state["jira_webhook_secret"] = secret
    ctx.run.secrets.add(secret)
    await ctx.world.call("/jira/register", url=url, secret=secret)
    conns = await ctx.admin.get("/api/v1/admin/connections")
    jira = [
        c
        for c in conns.body.get("items", conns.body if isinstance(conns.body, list) else [])
        if c.get("provider") == "jira"
    ]
    key = jira[0]["connection"] if jira else f"jira:{host}"
    await ctx.world.call(
        "/jira/issue",
        key="PAY-101",
        summary="Second approver for large refunds",
        description="Decision: refunds over 500 EUR need a second approver in the back office "
        "before refund_charge runs.",
        reporter="tomas",
        issuetype="Story",
    )
    await ctx.world.call(
        "/jira/issue",
        key="PAY-102",
        summary="Crypto payments",
        description="Idea: maybe we could support crypto payments someday.",
        reporter="dev",
        issuetype="Task",
    )
    ctx.sent.append({"jira": ["PAY-101", "PAY-102"]})
    await ctx.settle()
    held = await ctx.admin.records(state="held")
    ctx.observe("held records", held)
    r101 = [
        r["id"] for r in held if "second approver" in r["summary"].lower() or "500" in r["summary"]
    ]
    r102 = [r["id"] for r in held if "crypto" in r["summary"].lower()]
    ids101 = await ctx.records_from("second approver in the back office")
    ids102 = await ctx.records_from("crypto payments someday")
    r101 = list(dict.fromkeys(r101 + ids101))
    r102 = list(dict.fromkeys(r102 + ids102))
    ctx.observe("records per ticket", {"PAY-101": r101, "PAY-102": r102})
    texts = await record_texts(ctx, r101 + r102)
    leaked: dict[str, list[str]] = {}
    for who in EVERYONE:
        a = ctx.person(who)
        for q in ("refund second approver back office", "crypto payments"):
            s = await a.search(q, PAY, limit=50)
            d = await a.active_decisions()
            for rid in r101 + r102:
                if hit(s.body, rid, texts.get(rid)) or hit(d.body, rid, texts.get(rid)):
                    leaked.setdefault(who, []).append(rid)
    ctx.must("neither is readable by any agent while held", not leaked, leaked)
    c = await ctx.console("ingrid")
    review = await c.get("/api/v1/admin/review", connection=key)
    ctx.observe("Review page", review.body)
    listed = review.text
    ctx.must(
        "the Review page lists what is held",
        review.ok and bool(r101) and all(i in listed for i in r101 + r102),
        {"status": review.status, "held": r101 + r102},
    )
    if r101:
        rel = await admin_action(
            ctx,
            "release PAY-101",
            ("POST", "/api/v1/admin/review/release", {"record_id": r101[0]}),
            ["review", "release", "--record", r101[0]],
        )
        ctx.must("PAY-101's record is released", rel["ok"], rel)
        await ctx.settle()
        detail = (await ctx.admin.record(r101[0])).body
        ctx.observe("released record audience", detail.get("audience"))
        ctx.must(
            "PAY-101's record is a reference record",
            (detail.get("context_type") or "general") == "general",
            detail.get("context_type"),
        )

        async def readers() -> dict[str, bool]:
            return {
                who: await can_read(
                    ctx, who, r101[0], texts.get(r101[0]), "refund second approver back office"
                )
                for who in EVERYONE
            }

        got = await readers()
        ctx.observe("who reads PAY-101 after release", got)
        ctx.must("after release nobody can read it", not any(got.values()), got)
        doctor = await ctx.cli("doctor")
        warned = [
            line
            for line in doctor_lines(doctor.text).get("WARN", [])
            if key in line and "readable by nobody" in line
        ]
        ctx.must("the doctor warns that nobody can read it", bool(warned), doctor.text[-1500:])
        group = "payments-team"
        act = await admin_action(
            ctx,
            "set the audience of project PAY",
            (
                "PUT",
                "/api/v1/admin/connections/audience",
                {"connection": key, "container": "PAY", "mode": "groups", "groups": [group]},
            ),
            ["connections", "audience", key, "--container", "PAY", "--group", group],
        )
        ctx.must("the audience change is accepted", act["ok"], act)
        members = {"tomas", "aiko", "ingrid"}

        async def settled() -> dict[str, bool] | None:
            got = await readers()
            return got if {w for w, ok in got.items() if ok} == members else None

        final, waited = await ctx.wait_for(settled, 60, every=3)
        got = final or await readers()
        ctx.observe("who reads PAY-101 after the audience change", got)
        ctx.observe("seconds until the audience applied", waited)
        ctx.must(
            "payments-team's three members can read it and the others cannot",
            {w for w, ok in got.items() if ok} == members,
            got,
        )
    if r102:
        events = await ctx.run_sql(
            "SELECT event_id FROM events WHERE content_raw ILIKE '%crypto payments someday%'"
        )
        searches_before = await jira_searches(ctx)
        rej = await admin_action(
            ctx,
            "reject PAY-102",
            ("POST", "/api/v1/admin/review/reject", {"record_id": r102[0]}),
            ["review", "reject", "--record", r102[0], "--yes"],
        )
        await ctx.settle()
        gone = (await ctx.admin.record(r102[0])).status == 404
        left = await ctx.run_sql(
            "SELECT event_id FROM events WHERE content_raw ILIKE '%crypto payments someday%'"
        )
        ctx.must(
            "rejecting PAY-102 deletes the record and its events",
            rej["ok"] and gone and not left,
            {"before": events, "after": left},
        )
        if not left:
            ctx.run.ledger.removed["jira"] += len(events)

        async def polled() -> int | None:
            n = await jira_searches(ctx)
            return n if n > searches_before else None

        seen, waited = await ctx.wait_for(polled, 90, every=3)
        await ctx.settle()
        back = await ctx.run_sql(
            "SELECT event_id FROM events WHERE content_raw ILIKE '%crypto payments someday%'"
        )
        ctx.observe(
            "Jira poll after the rejection",
            {"searches_before": searches_before, "searches_after": seen, "waited": waited},
        )
        ctx.must(
            "the rejected ticket does not come back with the next poll",
            seen is not None and not back,
            {"polled": seen is not None, "events": back},
        )
    entries = await audit_since(ctx, start)
    actions = [e["action"] for e in entries]
    ctx.must(
        "release, reject and the audience change are audited",
        {"review.released", "review.rejected", "connection.audience_changed"} <= set(actions),
        sorted(set(actions)),
    )


@scenario("E08", "Scope keeps a channel out")
async def e08(ctx: Ctx) -> None:
    """Exclude #incidents from the Slack connection; Omar posts a decision there and one in #eng.

    must: nothing from #incidents is stored (no event row, no record, nothing sent to the model);
    stats show one event skipped by scope; the #eng message becomes a record.
    """
    channel = company.channel("incidents").id
    act = await admin_action(
        ctx,
        "exclude #incidents",
        (
            "PUT",
            "/api/v1/admin/connections/scope",
            {"connection": SLACK_CONNECTION, "exclude": [channel]},
        ),
        ["connections", "scope", SLACK_CONNECTION, "exclude", channel],
    )
    ctx.must("the scope change is accepted", act["ok"], act)
    ctx.run.excluded.add("incidents")
    await asyncio.sleep(11)
    model_from = (await ctx.world.get("/model"))["bodies"]
    before = await ctx.admin.stats()
    phrase = "page the payments on-call for any 5xx rate above 2%"
    await ctx.post("incidents", "omar", f"Decision: {phrase}.")
    await ctx.post(
        "eng",
        "omar",
        "Decision: the p99 latency SLO for the checkout API is 300 ms, measured over 28 days.",
    )
    await ctx.settle()
    rows = await ctx.run_sql(
        "SELECT event_id FROM events WHERE content_raw ILIKE '%5xx rate above 2%' "
        f"OR audience_container = '{channel}' "
        f"OR metadata->'chat_source'->>'channel_id' = '{channel}'"
    )
    ctx.must("no event row from #incidents", not rows, rows)
    ids = await ctx.records_from("5xx rate above 2")
    held = [r for r in await ctx.admin.records(q="5xx")]
    ctx.must("no record from #incidents", not ids and not held, {"linked": ids, "search": held})
    model = await ctx.world.get("/model", since=model_from)
    seen = [b for b in model.get("log", []) if "5xx rate above 2" in json.dumps(b["body"])]
    ctx.must("the model saw no request containing its text", not seen, len(seen))
    after = await ctx.admin.stats()
    skipped = after.get("scope_skipped", 0) - before.get("scope_skipped", 0)
    ctx.must(
        "stats show one event skipped by scope",
        skipped == 1,
        {
            "before": before.get("scope_skipped"),
            "after": after.get("scope_skipped"),
            "connections": after.get("connections"),
        },
    )
    eng = await one_record(ctx, "p99 latency SLO for the checkout API")
    ctx.must("the #eng message becomes a record", len(eng) == 1, eng)


@scenario("E09", "Leaving a channel removes access")
async def e09(ctx: Ctx) -> None:
    """Aiko leaves #payments-private and is later added back.

    must: within 30 seconds of the event Aiko can no longer read the E02 record by any route;
    after re-adding they can again; each refresh is in the audit log.
    observe: seconds from the event to the loss of access.
    """
    start = now_iso()
    rid, text = known(ctx, "E02")
    if not rid:
        ctx.must("the E02 record exists", False, "E02 made no record")
        return
    q = "chargeback evidence vault bucket retention"
    before = await can_read(ctx, "aiko", rid, text, q)
    ctx.observe("Aiko reads it before leaving", before)
    await ctx.world.call(
        "/slack/membership", channel="payments-private", person="aiko", joined=False
    )
    ctx.sent.append({"slack": "payments-private", "membership": "aiko left"})
    left_at = time.time()

    async def lost() -> bool:
        routes = await read_routes(ctx, "aiko", rid, text, query=q, symbols=("refund_charge",))
        return not any_true(routes)

    gone, waited = await ctx.wait_for(lost, 60, every=3)
    seconds = round(time.time() - left_at, 1)
    ctx.observe("seconds from the event to loss of access", seconds if gone else None)
    ctx.must(
        "within 30 seconds Aiko can no longer read it by any route",
        before and bool(gone) and seconds <= 30,
        {"seconds": seconds, "could read before leaving": before},
    )
    await ctx.world.call(
        "/slack/membership", channel="payments-private", person="aiko", joined=True
    )
    ctx.sent.append({"slack": "payments-private", "membership": "aiko joined"})

    async def back() -> bool:
        return await can_read(ctx, "aiko", rid, text, q)

    again, waited = await ctx.wait_for(back, 120, every=3)
    ctx.observe("seconds from re-adding to access", waited)
    ctx.must("after re-adding they can read it again", bool(again), {"waited": waited})
    await ctx.settle()
    entries = await audit_since(ctx, start, action="audience.refreshed")
    ctx.observe("audience refresh entries", entries)
    ctx.must("each refresh is in the audit log", len(entries) >= 2, len(entries))


@scenario("E10", "Offboarding through SCIM")
async def e10(ctx: Ctx) -> None:
    """Kofi posts a decision and signs in; the IdP deactivates (Entra PATCH), then deletes them.

    must: the first request with their key after deactivation is 401; their browser session is
    ended; after deletion the events they wrote are gone and records that depended only on them
    are gone; an audit trail remains without their message text.
    """
    start = now_iso()
    phrase = "checkout renders amounts only through formatMoney"
    await ctx.post(
        "eng",
        "kofi",
        f"Decision: web-app {phrase}; never build currency strings by hand in components.",
    )
    await ctx.settle()
    ids = await one_record(ctx, phrase)
    ctx.observe("Kofi's records before offboarding", ids)
    events = await event_ids(ctx, phrase)
    kofi = ctx.run.state["people"]["kofi"]
    browser = await ctx.console("kofi")
    me = await browser.get("/api/v1/auth/me")
    ctx.observe("Kofi's console session before", me.status)
    r = await ctx.run.scim.scim("PATCH", f"/Users/{kofi['scim_id']}", ENTRA_DEACTIVATE)
    ctx.observe("SCIM deactivate", {"status": r.status})
    first = await ctx.person("kofi").active_decisions()
    ctx.must(
        "the first request with their key after deactivation is 401",
        first.status == 401,
        first.status,
    )
    me = await browser.get("/api/v1/auth/me")
    ctx.must("their browser session is ended", me.status == 401, me.status)
    r = await ctx.run.scim.scim("DELETE", f"/Users/{kofi['scim_id']}")
    ctx.observe("SCIM delete", {"status": r.status})
    await ctx.settle()
    left = await event_ids(ctx, phrase)
    ctx.must(
        "the events they wrote are gone",
        bool(events) and not left,
        {"before": events, "after": left},
    )
    remaining = [i for i in ids if (await ctx.admin.record(i)).status != 404]
    ctx.must("records that depended only on them are gone", not remaining, remaining)
    if not left:
        theirs = [m for m in ctx.run.ledger.messages if m.get("person") == "kofi"]
        ctx.run.ledger.removed["slack"] += sum(1 for m in theirs if m["counted"])
        for m in theirs:
            m["removed"] = True
    entries = await audit_since(ctx, start)
    about = [e for e in entries if e["action"].startswith("user.")]
    ctx.observe("audit entries about them", about)
    ctx.must(
        "an audit trail remains",
        any(e["action"] == "user.deleted" for e in entries),
        [e["action"] for e in entries],
    )
    dumped = json.dumps(entries)
    ctx.must(
        "the audit trail carries no message text of theirs",
        "formatMoney" not in dumped and phrase not in dumped,
        None,
    )


@scenario("E11", "Single sign-on")
async def e11(ctx: Ctx) -> None:
    """Priya (admin group) and Dev (no group) sign in through the IdP; a stranger tries.

    must: Priya's session uses admin routes with the CSRF header and is refused without it;
    Dev's session gets 403 on admin routes and reads exactly what their key reads; the stranger is
    refused and lands on the console sign-in page with an error; sign-in and refusal are audited.
    """
    start = now_iso()
    priya = await ctx.console("priya")
    ctx.observe("Priya lands on", priya.landing)
    users = await priya.get("/api/v1/admin/users")
    ctx.must("Priya's session can read admin routes", users.ok, users.status)
    w = await priya.post("/api/v1/admin/audiences/refresh", {"all": True})
    ctx.must("Priya's session can write with the CSRF header", w.ok, {"status": w.status})
    nc = await priya.without_csrf("POST", "/api/v1/admin/audiences/refresh", {"all": True})
    ctx.must("Priya's session is refused without the CSRF header", nc.status == 403, nc.status)
    dev = await ctx.console("dev")
    a = await dev.get("/api/v1/admin/users")
    ctx.must("Dev's session gets 403 on admin routes", a.status == 403, a.status)
    mine = await dev.graph("decisions/active", repo_identifier=company.WEB_APP)
    key = await ctx.person("dev").graph("decisions/active", repo_identifier=company.WEB_APP)
    ctx.run.note_read("dev@" + company.DOMAIN, "decisions/active", mine)

    def ids_of(r: Reply) -> list[str]:
        body = r.body if isinstance(r.body, dict) else {}
        return sorted(
            norm(d.get("summary") or d.get("content") or "") for d in body.get("decisions", [])
        )

    same = mine.ok and key.ok and (mine.body == key.body or ids_of(mine) == ids_of(key))
    ctx.must(
        "Dev's session reads exactly what their key reads",
        same,
        {"session": mine.body, "key": key.body},
    )
    from sim.client import Console
    from sim.harness import world_port

    stranger = Console(ctx.run.calls, "stranger@elsewhere.example", world_port())
    try:
        landing = await stranger.sign_in()
    finally:
        await stranger.close()
    ctx.observe("stranger lands on", landing)
    ctx.must(
        "the stranger is refused and lands on the sign-in page with an error",
        landing.startswith("http://localhost:3410/sign-in") and "error=" in landing,
        landing,
    )
    entries = await audit_since(ctx, start)
    actions = [
        (e["action"], e.get("user_email")) for e in entries if e["action"].startswith("auth.")
    ]
    ctx.observe("auth audit entries", actions)
    ok = (
        ("auth.signed_in", "priya@marrowfield.example") in actions
        and ("auth.signed_in", "dev@marrowfield.example") in actions
        and any(a == "auth.sign_in_refused" for a, _ in actions)
    )
    ctx.must("sign-in and refusal are audited", ok, actions)


@scenario("E12", "A stated replacement")
async def e12(ctx: Ctx) -> None:
    """Tomas states 3 webhook retries; a simulated day later they supersede it with 8 retries.

    must: two records and one open suggestion with basis stated; until an admin acts both are
    served and the older carries may_be_replaced_by; after the admin accepts, the older is served
    to no agent and cited in no pull-request check; restore brings it back.
    """
    day = time.time() - 86400
    await ctx.post(
        "eng",
        "tomas",
        "Decision: payments-api sends webhooks to merchants with at most 3 retries.",
        at=day,
    )
    await ctx.settle()
    await ctx.post(
        "eng",
        "tomas",
        "Decision update: merchant webhooks now retry 8 times over 24 hours with exponential "
        "backoff. This supersedes the 3-retry decision.",
    )
    await ctx.settle()
    old = await one_record(ctx, "with at most 3 retries")
    new = await one_record(ctx, "retry 8 times over 24 hours")
    both = list(dict.fromkeys(old + new))
    ctx.must(
        "two records",
        len(both) == 2 and len(old) == 1 and len(new) == 1,
        {"older": old, "newer": new},
    )
    if not old or not new or old == new:
        return
    o, n = old[0], new[0]
    texts = await record_texts(ctx, [o, n])

    async def suggestion() -> list[dict[str, Any]]:
        r = await ctx.admin.get("/api/v1/admin/records/suggestions", limit=200)
        items = r.body.get("items", []) if r.ok else []
        return [s for s in items if s["record_id"] == o and s["replacement_id"] == n]

    found, _ = await ctx.wait_for(suggestion, 90, every=5)
    found = found or []
    ctx.observe("suggestions", found)
    ctx.must(
        "one open suggestion with basis stated",
        len(found) == 1 and found[0]["basis"] == "stated",
        found,
    )
    served: dict[str, Any] = {}
    for who in ("tomas", "rosa", "dev"):
        s = await ctx.person(who).search("merchant webhook retries", PAY)
        served[who] = {"older": hit(s.body, o, texts[o]), "newer": hit(s.body, n, texts[n])}
        rows = s.body.get("results", []) if isinstance(s.body, dict) else []
        served[who]["older may_be_replaced_by"] = any(
            r.get("id") == o and r.get("may_be_replaced_by") for r in rows
        )
    ctx.must(
        "until an admin acts both are served",
        all(v["older"] and v["newer"] for v in served.values()),
        served,
    )
    detail = (await ctx.admin.record(o)).body
    ctx.must(
        "the older one carries may_be_replaced_by",
        bool(detail.get("may_be_replaced_by"))
        and any(v["older may_be_replaced_by"] for v in served.values()),
        {"admin": detail.get("may_be_replaced_by"), "served": served},
    )
    if not found:
        return
    acc = await admin_action(
        ctx,
        "accept suggestion",
        ("POST", f"/api/v1/admin/records/suggestions/{found[0]['id']}/accept", {}),
        ["records", "accept", found[0]["id"]],
    )
    ctx.must("the admin accepts the suggestion", acc["ok"], acc)
    await ctx.settle()
    after: dict[str, Any] = {}
    for who in EVERYONE:
        a = ctx.person(who)
        s = await a.search("merchant webhook retries", PAY, limit=50)
        d = await a.active_decisions(PAY)
        after[who] = hit(s.body, o, texts[o]) or hit(d.body, o, texts[o])
    ctx.must("after accepting, the older one is served to no agent", not any(after.values()), after)
    webhooks = company.PAYMENTS_FILES["payments_api/webhooks.py"]
    pr = await open_pull(
        ctx,
        PAY,
        "tomas",
        "Raise merchant webhook retries",
        {"payments_api/webhooks.py": webhooks.replace("MAX_RETRIES = 3", "MAX_RETRIES = 8")},
    )
    status = await wait_status(ctx, PAY, pr["head_sha"])
    await asyncio.sleep(3)
    comments = await comments_on(ctx, PAY, pr["number"])
    ctx.observe("check on a webhooks.py change", {"status": status, "comments": comments})
    ctx.must(
        "the older one is cited in no pull-request check",
        not any(hit(c, o, texts[o]) for c in comments),
        comments,
    )
    res = await admin_action(
        ctx,
        "restore the older record",
        ("POST", f"/api/v1/admin/records/{o}/restore", {}),
        ["records", "restore", o],
    )
    await ctx.settle()
    back = await ctx.person("tomas").search("merchant webhook retries", PAY)
    ctx.must(
        "restore brings it back",
        res["ok"] and hit(back.body, o, texts[o]),
        {"action": res, "served": hit(back.body, o, texts[o])},
    )


@scenario("E13", "A key's life")
async def e13(ctx: Ctx) -> None:
    """Priya creates a key bound to Dev in the console, Dev uses it, Priya revokes it.

    must: the key value is returned once and never again by any list; it works; after revocation
    it gets 401 at once; creation, use and revocation are audited with Priya and Dev named.
    """
    start = now_iso()
    priya = await ctx.console("priya")
    made = await priya.post(
        "/api/v1/admin/keys",
        {"name": "dev-laptop", "admin": False, "user_email": "dev@marrowfield.example"},
    )
    ctx.must("Priya's console creates the key", made.status == 201, made.status)
    if made.status != 201:
        return
    value, kid = made.body["key"], made.body["id"]
    ctx.run.secrets.add(value)
    lists = [await priya.get("/api/v1/admin/keys"), await ctx.admin.get("/api/v1/admin/keys")]
    cli_list = await ctx.cli("keys", "list")
    again = [r.text for r in lists] + [cli_list.text]
    ctx.must(
        "the value is never returned again by any list", not any(value in t for t in again), None
    )
    from sim.harness import LoggedAgent

    temp = LoggedAgent(ctx.run.http, ctx.run.calls, "dev@marrowfield.example", value)
    temp.run = ctx.run
    used = await temp.active_decisions(company.WEB_APP)
    ctx.must(
        "the key works",
        used.ok and not (isinstance(used.body, dict) and "error" in used.body),
        used.status,
    )
    rv = await priya.post(f"/api/v1/admin/keys/{kid}/revoke")
    ctx.must("Priya revokes it", rv.ok, rv.status)
    after = await temp.active_decisions(company.WEB_APP)
    ctx.must("after revocation it gets 401 at once", after.status == 401, after.status)
    entries = await audit_since(ctx, start)
    created = [e for e in entries if e["action"] == "key.created"]
    used_e = [e for e in entries if e.get("key_id") == kid and e["action"].startswith("mcp.")]
    revoked = [e for e in entries if e["action"] == "key.revoked"]
    ctx.observe("audit entries", [e for e in entries if e in created + used_e + revoked])
    priya_named = all(e.get("user_email") == "priya@marrowfield.example" for e in created + revoked)
    dev_named = bool(created) and "dev@marrowfield.example" in json.dumps(created)
    use_named = bool(used_e) and all(
        e.get("user_email") == "dev@marrowfield.example" for e in used_e
    )
    ctx.must(
        "creation, use and revocation are audited with Priya and Dev named",
        bool(created) and bool(revoked) and priya_named and dev_named and use_named,
        {
            "Priya named on creation and revocation": priya_named,
            "Dev named on creation": dev_named,
            "Dev named on each use": use_named,
            "created": created,
            "used": used_e,
            "revoked": revoked,
        },
    )


@scenario("E14", "Deleting a source")
async def e14(ctx: Ctx) -> None:
    """Dry run, then the real purge, of everything from #random plus one #eng thread by event id.

    must: the dry run changes nothing and its counts match what the real purge removes; afterwards
    no event, record, excerpt or search hit remains; a context-file update is proposed if a purged
    record was in the file; the purge is audited.
    """
    start = now_iso()
    phrase = "card numbers never touch localStorage"
    root = await ctx.post(
        "eng", "rosa", f"Decision: in web-app {phrase}; checkout keeps them in memory only."
    )
    await ctx.post("eng", "dev", "ok, I'll move the cache.", thread_ts=root)
    await ctx.settle()
    thread_records = await one_record(ctx, phrase)
    thread_events = await event_ids(ctx, phrase, "move the cache")
    container = company.channel("random").id
    random_events = [
        r[0]
        for r in await ctx.run_sql(
            "SELECT event_id FROM events WHERE source_platform = 'slack' "
            f"AND audience_container = '{container}'"
        )
    ]
    random_records = sorted(
        {
            r[0]
            for r in await ctx.run_sql(
                "SELECT l.canonical_id FROM context_event_links l "
                "JOIN events e ON e.event_id = l.event_id "
                f"WHERE e.source_platform = 'slack' AND e.audience_container = '{container}'"
            )
        }
    )
    ctx.track(*thread_records, *random_records)
    ctx.observe(
        "selected",
        {
            "container": container,
            "random events": len(random_events),
            "thread events": thread_events,
            "thread records": thread_records,
            "random records": random_records,
        },
    )
    files = {}
    for repo in (company.WEB_APP, PAY):
        files[repo] = (
            await ctx.world.get(
                "/github/file",
                repo=repo,
                ref="checkcord/update-context",
                path=".context/ARCHITECTURE.md",
            )
        )["text"]
    texts = await record_texts(ctx, thread_records + random_records)
    in_file = {repo: [i for i in texts if f and hit(f, i, texts[i])] for repo, f in files.items()}
    writes_before = {
        repo: len((await ctx.world.get("/github/received", repo=repo))["content_writes"])
        for repo in files
    }

    async def counts() -> dict[str, int]:
        ev = await ctx.run_sql("SELECT count(*) FROM events")
        rc = await ctx.run_sql("SELECT count(*) FROM canonical_contexts")
        ln = await ctx.run_sql("SELECT count(*) FROM context_event_links")
        return {"events": int(ev[0][0]), "records": int(rc[0][0]), "links": int(ln[0][0])}

    async def purge(kind: str, dry: bool) -> dict[str, Any]:
        if kind == "source":
            console = (
                "POST",
                "/api/v1/admin/purge/source",
                {"platform": "slack", "container": container, "dry_run": dry},
            )
            cli = ["purge", "source", "--platform", "slack", "--container", container]
        else:
            console = (
                "POST",
                "/api/v1/admin/purge/event",
                {"event_ids": thread_events, "dry_run": dry},
            )
            cli = ["purge", "event"] + [x for e in thread_events for x in ("--id", e)]
        cli.append("--dry-run" if dry else "--yes")
        res = await admin_action(ctx, f"purge {kind}{' (dry run)' if dry else ''}", console, cli)
        if res["via"] == "console":
            body = res.get("body") or {}
            got = body.get("purged", body) if isinstance(body, dict) else {}
            res["counts"] = {k: v for k, v in got.items() if isinstance(v, int)}
        else:
            res["counts"] = cli_counts(res["output"])
        return dict(res)

    before = await counts()
    dry_source = await purge("source", True)
    dry_event = await purge("event", True) if thread_events else {"counts": {}}
    unchanged = await counts()
    ctx.must(
        "the dry run changes nothing", before == unchanged, {"before": before, "after": unchanged}
    )
    real_source = await purge("source", False)
    real_event = await purge("event", False) if thread_events else {"counts": {}}
    await ctx.settle()
    after = await counts()
    removed = {
        "events": before["events"] - after["events"],
        "records": before["records"] - after["records"],
    }
    dry_total = {
        "events": dry_source["counts"].get("events", 0) + dry_event["counts"].get("events", 0),
        "records": dry_source["counts"].get("records_deleted", 0)
        + dry_event["counts"].get("records_deleted", 0),
    }
    real_total = {
        "events": real_source["counts"].get("events", 0) + real_event["counts"].get("events", 0),
        "records": real_source["counts"].get("records_deleted", 0)
        + real_event["counts"].get("records_deleted", 0),
    }
    ctx.must(
        "the dry-run counts match what the real purge removes",
        dry_total == real_total == removed,
        {
            "dry run": dry_total,
            "real purge": real_total,
            "removed from the database": removed,
            "dry": [dry_source["counts"], dry_event["counts"]],
            "real": [real_source["counts"], real_event["counts"]],
        },
    )
    left_events = await ctx.run_sql(
        "SELECT event_id FROM events WHERE source_platform = 'slack' "
        f"AND audience_container = '{container}'"
    ) + [[e] for e in await event_ids(ctx, phrase, "move the cache")]
    left_records = [
        i for i in thread_records + random_records if (await ctx.admin.record(i)).status != 404
    ]
    left_links = await ctx.run_sql(
        "SELECT canonical_id FROM context_event_links "
        "WHERE excerpt_redacted ILIKE '%localStorage%' "
        "OR excerpt_redacted ILIKE '%coffee machine%'"
    )
    s = await ctx.person("rosa").search(
        "card numbers localStorage checkout memory", company.WEB_APP
    )
    search_hit = [i for i in texts if hit(s.body, i, texts[i])]
    ctx.must(
        "no event, record, excerpt or search hit remains",
        not left_events and not left_records and not left_links and not search_hit,
        {
            "events": left_events,
            "records": left_records,
            "excerpts": left_links,
            "search": search_hit,
        },
    )
    if not left_events:
        purged_msgs = [
            m
            for m in ctx.run.ledger.messages
            if m["channel"] == "random" or m["ts"] == root or m.get("thread_ts") == root
        ]
        ctx.run.ledger.removed["slack"] += sum(1 for m in purged_msgs if m["counted"])
        for m in purged_msgs:
            m["removed"] = True
    for repo, ids in in_file.items():
        if not ids:
            continue

        async def rewritten(repo: str = repo, ids: list[str] = ids) -> bool:
            got = await ctx.world.get("/github/received", repo=repo)
            writes = got["content_writes"][writes_before[repo] :]
            return any(
                w["path"] == ".context/ARCHITECTURE.md"
                and not any(hit(w["text"], i, texts[i]) for i in ids)
                for w in writes
            )

        ok, _ = await ctx.wait_for(rewritten, 90, every=5)
        ctx.must(
            f"a context-file update without the purged records is proposed for {repo}",
            bool(ok),
            ids,
        )
    ctx.observe("purged records that were in a context file", in_file)
    entries = await audit_since(ctx, start, action="data.purged")
    ctx.observe("purge audit entries", entries)
    ctx.must("the purge is audited", len(entries) >= 2, len(entries))


@scenario("E15", "The model goes down and comes back")
async def e15(ctx: Ctx) -> None:
    """The model stand-in answers 500 for 90 seconds while three decisions are posted in #eng.

    must: no event is lost; while it is down the failed list or the pending count shows them and
    doctor warns; after recovery all three become records with no duplicates and nobody acting;
    the outage shows as failed calls in usage.
    """
    usage_before = await ctx.admin.usage()
    await ctx.world.call("/model/mode", mode="down")
    down_at = time.time()
    phrases = [
        "refund_charge posts a negative ledger entry and never edits the original",
        "mask_card is applied to every log line that may carry a card number",
        "verify_signature compares digests with hmac.compare_digest only",
    ]
    for p, who in zip(phrases, ("tomas", "rosa", "omar"), strict=True):
        await ctx.post("eng", who, f"Decision: {p}.")
    await asyncio.sleep(45)
    stats = await ctx.admin.stats()
    failed = await ctx.cli("events", "failed")
    doctor = await ctx.cli("doctor")
    discussions = stats.get("discussions") or {}
    pending = discussions.get("waiting", 0)
    buffered = discussions.get("buffered", 0)
    ctx.observe("while down: stats discussions", stats.get("discussions"))
    ctx.observe("while down: stats unfinished", stats.get("events_unfinished"))
    ctx.observe("while down: checkcord events failed", failed.text)
    ctx.observe("while down: checkcord doctor", doctor.text)
    shown = pending >= 1 or buffered >= 1 or any(p.split()[0] in failed.out for p in phrases)
    ctx.must(
        "while down, the failed list or the pending count shows them",
        shown,
        {"pending": pending, "buffered": buffered},
    )
    lines = doctor_lines(doctor.text)
    about_model = [
        line
        for line in lines.get("WARN", []) + lines.get("FAIL", [])
        if any(w in line.lower() for w in ("llm", "model", "embedding"))
    ]
    ctx.must("doctor warns about the model while it is down", bool(about_model), lines)
    rest = 90 - (time.time() - down_at)
    if rest > 0:
        await asyncio.sleep(rest)
    await ctx.world.call("/model/mode", mode="ok")
    await ctx.settle(900)
    events: dict[str, Any] = {}
    records: dict[str, list[str]] = {}
    for p in phrases:
        events[p] = await ctx.events_with(p[:40])
        records[p] = await ctx.records_from(p[:40])
    lost = [p for p, e in events.items() if not e]
    ctx.must("no event is lost", not lost, events)
    ctx.must(
        "after recovery all three become records with no duplicates",
        all(len(r) == 1 for r in records.values()),
        records,
    )
    usage_after = await ctx.admin.usage()
    fb = (usage_before.get("totals") or {}).get("failures", 0)
    fa = (usage_after.get("totals") or {}).get("failures", 0)
    ctx.observe(
        "usage totals", {"before": usage_before.get("totals"), "after": usage_after.get("totals")}
    )
    ctx.must("the outage shows as failed calls in usage", fa > fb, {"before": fb, "after": fa})


@scenario("E16", "Custom sources")
async def e16(ctx: Ctx) -> None:
    """Push API event for Rosa and Ingrid only; signed webhook events: good, wrong signature, late.

    must: the pushed record is readable by Rosa and Ingrid only; the signed event becomes a record;
    the wrong signature and the late replay are refused with 401 and create nothing.
    """
    people = ctx.run.state["people"]
    allowed = [people["rosa"]["user_id"], people["ingrid"]["user_id"]]
    pushed = "the security review rota rotates weekly between Rosa and Ingrid"
    push = await ctx.admin.post(
        "/api/v1/ingest/custom",
        {
            "source_platform": "custom",
            "source_type": "custom_payload",
            "content_raw": f"Decision: {pushed}, and findings go to the private tracker.",
            "author": {"source_id": "security-tool", "display_name": "Security tooling"},
            "access_control": {
                "is_public": False,
                "visibility": "restricted",
                "acl_type": "custom",
                "allowed_identity_ids": allowed,
            },
        },
    )
    ctx.observe("push", {"status": push.status, "body": push.body})
    if push.ok:
        ctx.run.ledger.events["custom"] += 1
    res = await ctx.cli("connect", "webhook", "--name", "status-page")
    secret = secret_after("Signing secret", res.out)
    url = url_after("URL:", res.out)
    if not secret or not url:
        ctx.must("the signed webhook connects", False, res.text)
        return
    ctx.run.state["signed_secret"] = secret
    ctx.run.secrets.add(secret)
    url = url.replace("http://localhost:8410", API)

    async def send(item: dict[str, Any], *, sign_with: str, at: float | None = None) -> Reply:
        body = json.dumps(item).encode()
        r = await ctx.run.http.post(url, content=body, headers=signed_headers(sign_with, body, at))
        ctx.run.calls.add("status-page", "POST", "/api/v1/webhooks/signed/…", r.status_code)
        ctx.sent.append({"signed": item["id"], "status": r.status_code})
        try:
            parsed: Any = r.json()
        except ValueError:
            parsed = r.text
        return Reply(r.status_code, parsed, dict(r.headers))

    good_text = (
        "Decision: the status page shows a payments incident within 5 minutes of the first page."
    )
    good = await send(
        {"id": "sp-1", "text": good_text, "audience": {"public": True}}, sign_with=secret
    )
    if good.ok:
        ctx.run.ledger.events["custom"] += 1
    wrong = await send(
        {
            "id": "sp-2",
            "text": "Decision: wrong signature never lands.",
            "audience": {"public": True},
        },
        sign_with=secret[::-1],
    )
    late = await send(
        {"id": "sp-3", "text": "Decision: late replay never lands.", "audience": {"public": True}},
        sign_with=secret,
        at=time.time() - 600,
    )
    ctx.observe(
        "signed responses",
        {
            "good": [good.status, good.body],
            "wrong": [wrong.status, wrong.body],
            "late": [late.status, late.body],
        },
    )
    await ctx.settle()
    p_ids = await one_record(ctx, "security review rota rotates weekly")
    ctx.must("the pushed event becomes a record", len(p_ids) == 1, p_ids)
    if p_ids:
        text = (await record_texts(ctx, p_ids))[p_ids[0]]
        remember(ctx, "E16", p_ids[0], text, private=True)
        readers = {
            who: await can_read(ctx, who, p_ids[0], text, "security review rota weekly")
            for who in EVERYONE
        }
        ctx.must(
            "the pushed record is readable by Rosa and Ingrid only",
            readers.pop("rosa") and readers.pop("ingrid") and not any(readers.values()),
            readers,
        )
    s_ids = await one_record(ctx, "status page shows a payments incident")
    ctx.must(
        "the signed event becomes a record",
        good.status == 202 and len(s_ids) == 1,
        {"status": good.status, "records": s_ids},
    )
    nothing = await ctx.run_sql(
        "SELECT event_id FROM events WHERE content_raw ILIKE '%wrong signature never lands%' "
        "OR content_raw ILIKE '%late replay never lands%'"
    )
    ctx.must(
        "the wrong signature and the late replay are refused with 401 and create nothing",
        wrong.status == 401 and late.status == 401 and not nothing,
        {"wrong": wrong.status, "late": late.status, "events": nothing},
    )


@scenario("E17", "The context file")
async def e17(ctx: Ctx) -> None:
    """After E01, E03 and E05, the context-file job runs for payments-api.

    must: the stand-in GitHub receives one pull request from a checkcord/ branch adding
    .context/ARCHITECTURE.md; the file has the public records in redacted form and no record
    whose audience is narrower than the collaborators (the E02 and E16 records); none of the E03
    strings appear.
    observe: the file's full text.
    """

    async def pulls() -> list[dict[str, Any]]:
        got = await ctx.world.get("/github/received", repo=PAY)
        return [p for p in got["pulls"] if p["head"]["ref"].startswith("checkcord/")]

    found, waited = await ctx.wait_for(pulls, 180, every=5)
    found = found or []
    got = await ctx.world.get("/github/received", repo=PAY)
    writes = [w for w in got["content_writes"] if w["path"] == ".context/ARCHITECTURE.md"]
    ctx.observe(
        "context-file pull requests",
        [{"number": p["number"], "head": p["head"]["ref"], "title": p["title"]} for p in found],
    )
    ctx.must(
        "one pull request from a checkcord/ branch adds .context/ARCHITECTURE.md",
        len(found) == 1 and bool(writes),
        {"pulls": len(found), "writes": len(writes)},
    )
    if not found:
        return
    text = await ctx.world.get(
        "/github/file", repo=PAY, ref=found[0]["head"]["ref"], path=".context/ARCHITECTURE.md"
    )
    body = text.get("text") or ""
    ctx.observe("the file's full text", body)
    missing = []
    for name in ("E01", "E03", "E05"):
        rid, rtext = known(ctx, name)
        if not rid or not hit(body, rid, rtext):
            missing.append(name)
    ctx.must("the file contains the public records (E01, E03, E05)", not missing, missing)
    narrower = restricted_in(ctx, body)
    ctx.must(
        "the file has no record whose audience is narrower than the collaborators",
        not narrower,
        narrower,
    )
    if sensitive(ctx):
        bad = leaks(body, sensitive(ctx))
        ctx.must("the file carries none of the E03 strings", not bad, bad)


@scenario("E19", "Numbers add up")
async def e19(ctx: Ctx) -> None:
    """Compare checkcord stats, checkcord usage and an audit export with the runner's own log.

    must: the Slack chat messages kept equal the chat-message rows in the events table and lie
    between the runner's decision-bearing messages still stored and all its Slack messages sent
    and not removed (which chatter is kept is the model's judgement); other events by source,
    agent reads and distinct readers, pull-request checks and skipped-by-scope counts equal the
    runner's counts. The runner counts as decision-bearing every Slack message it posted to an
    in-scope channel without a subtype, except the chatter E04 expects the scrubber to drop, less
    the messages purges and offboarding removed. Other events are its pushed and signed custom
    events, pull requests opened or merged other than Checkcord's own, and accepted Jira
    deliveries, less what review rejected. Checks on Checkcord's own pull requests count as
    skipped. Outcome events Checkcord derives from chat are in neither count.
    observe: model calls and tokens by purpose.
    """
    await ctx.settle()
    since = ctx.run.state.get("run_started")
    stats = await ctx.admin.stats()
    cli_stats = await ctx.cli("stats", "--days", "2")
    cli_usage = await ctx.cli("usage", "--days", "2")
    export = await ctx.cli(
        "audit", "export", "--since", since or "1970-01-01T00:00:00Z", "--format", "jsonl"
    )
    ctx.observe("checkcord stats", cli_stats.text)
    ctx.observe("checkcord usage", cli_usage.text)
    rows = []
    for line in export.out.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    ledger = ctx.run.ledger
    deliveries = (await ctx.world.get("/deliveries"))["items"]
    accepted = [d for d in deliveries if 200 <= int(d.get("status") or 0) < 300]
    gh = [d for d in accepted if d["service"] == "github" and d["event"] == "pull_request"]
    own: set[tuple[str, int]] = set()
    for repo in sorted({d["repo"] for d in gh}):
        pulls = (await ctx.world.get("/github/received", repo=repo)).get("pulls") or []
        own |= {(repo, p["number"]) for p in pulls if p.get("by_checkcord")}
    from_source = [d for d in gh if (d["repo"], d.get("number")) not in own]
    decisions_kept = ledger.events["slack"] - ledger.removed["slack"]
    sent_kept = sum(1 for m in ledger.messages if m.get("stored") and not m.get("removed"))
    [[rows_kept]] = await ctx.run_sql(
        "SELECT count(*) FROM events "
        "WHERE source_platform = 'slack' AND source_id LIKE 'chat-message:%'"
    )
    others = {
        "custom": ledger.events["custom"],
        "github": sum(1 for d in from_source if d["action"] == "opened")
        + sum(1 for d in from_source if d["action"] == "closed" and d.get("merged")),
        "jira": sum(1 for d in accepted if d["service"] == "jira"),
    }
    for source, n in ledger.removed.items():
        if source != "slack":
            others[source] = others.get(source, 0) - n
    kept: dict[str, int] = {}
    stored: dict[str, int] = {}
    for e in stats.get("events", []):
        kept[e["source"]] = kept.get(e["source"], 0) + e["messages"]
        stored[e["source"]] = stored.get(e["source"], 0) + e["events"]
    breakdown = await ctx.run_sql(
        "SELECT source_platform, split_part(source_id, ':', 1), count(*) FROM events "
        "GROUP BY 1, 2 ORDER BY 1, 2"
    )
    sources = sorted({"slack"} | set(others) | set(kept) | set(stored))
    diff = {
        s: {
            **({} if s == "slack" else {"chat messages": {"runner": 0, "stats": kept.get(s, 0)}}),
            "other events": {"runner": others.get(s, 0), "stats": stored.get(s, 0)},
        }
        for s in sources
    }
    slack_kept = kept.get("slack", 0)
    slack = {
        "stats": slack_kept,
        "chat-message rows": int(rows_kept),
        "decision-bearing still stored": decisions_kept,
        "sent and not removed": sent_kept,
    }
    ctx.must(
        "Slack chat messages kept equal the chat-message rows and lie between the runner's "
        "decision-bearing and all sent messages; other events and other sources' chat messages "
        "equal the runner's counts",
        slack_kept == int(rows_kept)
        and decisions_kept <= slack_kept <= sent_kept
        and all(v["runner"] == v["stats"] for d in diff.values() for v in d.values()),
        {
            "slack chat messages": slack,
            "by source": diff,
            "events table": breakdown,
            "runner removed": dict(ledger.removed),
        },
    )
    reads = sum(d["reads"] for d in stats.get("agent_reads", []))
    from sim.harness import AGENT_READ_ACTIONS

    exported_reads = [r for r in rows if r.get("action") in AGENT_READ_ACTIONS]
    ctx.must(
        "agent reads equal the runner's count",
        reads == ledger.reads == len(exported_reads),
        {"runner": ledger.reads, "stats": reads, "audit export": len(exported_reads)},
    )
    readers = {(r.get("user_email") or "").lower() for r in exported_reads if r.get("user_email")}
    ctx.must(
        "distinct readers equal the runner's count",
        stats.get("people_reading") == len(ledger.readers) == len(readers),
        {
            "runner": sorted(ledger.readers),
            "stats": stats.get("people_reading"),
            "audit export": sorted(readers),
        },
    )
    checks = [d for d in gh if d["action"] in ("opened", "synchronize")]
    skipped_runner = sum(1 for d in checks if (d["repo"], d.get("number")) in own)
    exported_checks = [r for r in rows if r.get("action") == "pr.check_posted"]
    pr_stats = stats.get("pr_checks") or {}
    posted, skipped = pr_stats.get("posted"), pr_stats.get("skipped")
    ctx.must(
        "pull-request checks equal the runner's count, Checkcord's own pull requests as skipped",
        posted == len(checks) - skipped_runner
        and skipped == skipped_runner
        and len(checks) == len(exported_checks),
        {
            "runner": {"posted": len(checks) - skipped_runner, "skipped": skipped_runner},
            "stats": {"posted": posted, "skipped": skipped},
            "audit export": len(exported_checks),
            "own pull requests": sorted(own),
        },
    )
    ctx.must(
        "skipped-by-scope equals the runner's count",
        stats.get("scope_skipped") == ledger.skipped,
        {"runner": ledger.skipped, "stats": stats.get("scope_skipped")},
    )
    usage = await ctx.admin.usage()
    ctx.observe("model calls and tokens by purpose", usage.get("rows"))
    ctx.observe(
        "model stand-in counts",
        {k: v for k, v in (await ctx.world.get("/model")).items() if k != "log"},
    )


@scenario("E18", "Operations", last=True)
async def e18(ctx: Ctx) -> None:
    """Read /metrics on API and worker; 700 requests in a minute with one key; back up and restore.

    must: metrics carry no person, key or content; the burst gets 429 with Retry-After; after
    restore all records, keys, sessions, audit entries and stored credentials work and doctor
    passes.
    """
    from sim.client import worker_metrics

    api_metrics = (await ctx.run.http.get(API + "/metrics")).text
    worker = await worker_metrics()
    people = ctx.run.state["people"]
    needles: dict[str, str] = {}
    for p in company.PEOPLE:
        needles[f"{p.key} email"] = p.email
        needles[f"{p.key} name"] = p.name
        needles[f"{p.key} slack id"] = p.slack_id
        needles[f"{p.key} key name"] = f"sim-{p.key}"
        if p.key in people:
            needles[f"{p.key} key"] = people[p.key]["key"]
            needles[f"{p.key} key id"] = people[p.key]["key_id"]
    for phrase in (
        "Idempotency",
        "chargeback",
        "quote_total returns",
        "on-call escalation",
        "localStorage",
    ):
        needles[f"content {phrase}"] = phrase
    for name, rec in ctx.shared.get("records", {}).items():
        if rec.get("id"):
            needles[f"{name} record id"] = rec["id"]
    found = {
        n: [w for w, text in (("api", api_metrics), ("worker", worker)) if v in text]
        for n, v in needles.items()
    }
    found = {k: v for k, v in found.items() if v}
    ctx.must("metrics carry no person, key or content", not found, found)
    a = ctx.person("rosa")
    statuses: list[int] = []
    retry_after: list[str] = []
    t0 = time.time()

    async def one() -> None:
        r = await ctx.run.http.get(API + "/api/v1/graph/repositories", headers=a.headers())
        statuses.append(r.status_code)
        if r.status_code == 429 and r.headers.get("retry-after"):
            retry_after.append(r.headers["retry-after"])

    for _ in range(35):
        await asyncio.gather(*(one() for _ in range(20)))
    took = round(time.time() - t0, 1)
    counts = {s: statuses.count(s) for s in sorted(set(statuses))}
    ctx.observe(
        "burst",
        {
            "requests": len(statuses),
            "seconds": took,
            "statuses": counts,
            "retry_after": retry_after[:3],
        },
    )
    ctx.must(
        "the burst gets 429 with Retry-After", counts.get(429, 0) > 0 and bool(retry_after), counts
    )

    records_before = sorted(await all_record_ids(ctx.admin))
    audit_before = await ctx.admin.audit()
    audit_ids = {e["id"] for e in audit_before}
    dump = ctx.run.out / "backup.dump" if ctx.run.out else ROOT / "sim" / "out" / "backup.dump"
    with dump.open("wb") as fh:
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "postgres",
                "pg_dump",
                "-U",
                "checkcord",
                "-d",
                "checkcord",
                "-Fc",
            ],
            cwd=ROOT,
            stdout=fh,
            stderr=subprocess.PIPE,
        )
    ctx.must(
        "the backup is taken",
        proc.returncode == 0 and dump.stat().st_size > 0,
        proc.stderr.decode()[-500:],
    )
    steps: list[dict[str, Any]] = []

    async def step(*args: str, stdin: Any = None) -> int:
        if stdin is None:
            res = await asyncio.to_thread(compose, *args, timeout=1800)
            steps.append({"step": " ".join(args), "exit": res.code, "tail": res.text[-400:]})
            return res.code
        with stdin.open("rb") as fh:
            p = await asyncio.to_thread(
                subprocess.run,
                ["docker", "compose", *args],
                cwd=ROOT,
                stdin=fh,
                capture_output=True,
            )
        steps.append(
            {"step": " ".join(args), "exit": p.returncode, "tail": p.stderr.decode()[-400:]}
        )
        return p.returncode

    await step("down", "-v")
    await step("up", "-d", "--wait", "postgres", "redis")
    await step(
        "exec",
        "-T",
        "postgres",
        "pg_restore",
        "-U",
        "checkcord",
        "-d",
        "checkcord",
        "--exit-on-error",
        stdin=dump,
    )
    await step("up", "-d", "--wait")
    dump.unlink(missing_ok=True)
    ctx.observe("restore steps", steps)
    ctx.must("the restore steps succeed", all(s["exit"] == 0 for s in steps), steps)
    doctor = await ctx.cli("doctor")
    ctx.observe("doctor after restore", doctor.text)
    ctx.must(
        "doctor passes after restore",
        doctor.code == 0 and not doctor_lines(doctor.text).get("FAIL"),
        doctor.text[-1500:],
    )
    await asyncio.sleep(5)
    await ctx.settle()
    records_after = sorted(await all_record_ids(ctx.admin))
    ctx.must(
        "all records are back",
        records_after == records_before,
        {"before": len(records_before), "after": len(records_after)},
    )
    keys = {}
    for who in EVERYONE:
        if who == "kofi" or who in ctx.shared.get("deleted", []):
            continue
        r = await ctx.person(who).active_decisions()
        keys[who] = r.status
    ctx.must("every personal key works", all(v == 200 for v in keys.values()), keys)
    me = await (await ctx.console("ingrid")).get("/api/v1/auth/me")
    ctx.must("Ingrid's console session still works", me.ok, me.status)
    audit_after = {e["id"] for e in await ctx.admin.audit()}
    ctx.must(
        "every audit entry is back",
        audit_ids <= audit_after,
        {"missing": len(audit_ids - audit_after)},
    )
    health, _ = await ctx.wait_for(lambda: _slack_connected(ctx), 60, every=3)
    ctx.must("the stored Slack credentials reconnect Socket Mode", bool(health), None)
    probe = "Decision: the ops runbook lives in the handbook under engineering/on-call."
    await ctx.post("eng", "omar", probe)
    stored, _ = await ctx.wait_for(
        lambda: ctx.events_with("ops runbook lives in the handbook"), 240, every=5
    )
    ctx.must("Slack messages are stored again after restore", bool(stored), stored)
    web = company.WEB_FILES["src/format/money.ts"]
    pr = await open_pull(
        ctx,
        company.WEB_APP,
        "dev",
        "Format money with two decimals",
        {"src/format/money.ts": web + "\n"},
    )
    status = await wait_status(ctx, company.WEB_APP, pr["head_sha"], timeout=120)
    ctx.must("the stored GitHub token and webhook secret still work", bool(status), status)
    await ctx.settle()


async def _slack_connected(ctx: Ctx) -> bool:
    health = await ctx.world.get("/health")
    return bool(health.get("slack_connected"))

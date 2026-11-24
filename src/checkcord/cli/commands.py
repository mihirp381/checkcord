import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn
from uuid import UUID

from checkcord.cli import proxy


class CLIError(Exception):
    pass


def _fail(message: str) -> NoReturn:
    raise CLIError(message)


def _settings() -> Any:
    from checkcord.core.config import get_settings

    return get_settings()


def _tenant(args: argparse.Namespace) -> UUID:
    tenant: UUID | None = args.tenant or _settings().default_tenant_id
    if tenant is None:
        _fail("no tenant: pass --tenant UUID or set DEFAULT_TENANT_ID in the environment")
    return tenant


def _public_url(args: argparse.Namespace) -> str:
    url: str | None = args.public_url or _settings().public_base_url
    if not url:
        _fail(
            "no public URL: pass --public-url https://checkcord.example.com or set PUBLIC_BASE_URL"
        )
    return url.rstrip("/")


def _with_engine[T](fn: Callable[[Any], Awaitable[T]]) -> T:
    from checkcord.storage.postgres import create_checkcord_engine

    async def runner() -> T:
        engine = create_checkcord_engine(_settings().database_url)
        try:
            return await fn(engine)
        finally:
            await engine.dispose()

    return asyncio.run(runner())


def _with_redis[T](fn: Callable[[Any], Awaitable[T]]) -> T:
    from redis.asyncio import Redis

    async def runner() -> T:
        redis = Redis.from_url(_settings().redis_url)
        try:
            return await fn(redis)
        finally:
            await redis.aclose()

    return asyncio.run(runner())


def _record(tenant: UUID, action: str, **kwargs: Any) -> None:
    from checkcord.storage import audit
    from checkcord.storage.postgres import tenant_context

    async def run(engine: Any) -> None:
        async with tenant_context(engine, tenant) as conn:
            await audit.record(conn, tenant, audit.CLI_ACTOR, action, **kwargs)

    _with_engine(run)


def _age(seconds: float) -> str:
    seconds = int(seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _revocation_note() -> str:
    from checkcord.api.deps import TOKEN_CACHE_TTL_SECONDS

    return (
        f"Revocation reaches a running API within {TOKEN_CACHE_TTL_SECONDS} seconds "
        "(token cache TTL)."
    )


def _print_key(token: str, key_id: UUID, tenant: UUID, admin: bool) -> None:
    print(f"Created {'admin ' if admin else ''}API key {key_id} for tenant {tenant}.")
    print()
    print(f"    {token}")
    print()
    print("Store it now: it is not saved anywhere and cannot be shown again.")
    print('Use it as "Authorization: Bearer <key>".')


# Commands


def cmd_api(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run(
        "checkcord.main:app",
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips=args.forwarded_allow_ips or ",".join(_settings().forwarded_allow_ips),
    )


def cmd_worker(args: argparse.Namespace) -> None:
    from checkcord import worker

    worker.run(args.heartbeat_file)


def cmd_mcp(args: argparse.Namespace) -> None:
    from checkcord.proxy import mcp_server

    mcp_server.main()


def cmd_migrate(args: argparse.Namespace) -> None:
    from alembic.config import Config

    from alembic import command
    from checkcord.services.doctor import find_alembic_ini

    ini = find_alembic_ini()
    if ini is None:
        _fail("alembic.ini not found; run from the Checkcord source checkout or the image's /app")
    command.upgrade(Config(str(ini)), "head")

    from checkcord.storage import vector_column
    from checkcord.storage.postgres import create_checkcord_engine

    settings = _settings()

    async def sync() -> str | None:
        engine = create_checkcord_engine(settings.effective_migration_database_url)
        try:
            return await vector_column.sync(
                engine, settings.embedding_dimension, reset=args.reset_embeddings
            )
        finally:
            await engine.dispose()

    try:
        change = asyncio.run(sync())
    except vector_column.EmbeddingColumnError as e:
        _fail(str(e))
    if change:
        print(change)


def cmd_demo_load(args: argparse.Namespace) -> None:
    from checkcord.services import demo

    tenant = _tenant(args)
    settings = _settings()
    try:
        loaded = _with_engine_and_redis(lambda e, r: demo.load(e, r, tenant))
    except demo.SampleDataExistsError:
        _fail("sample data is already loaded; run `checkcord demo remove` first")
    print(f"Published {len(loaded.event_ids)} sample events.")
    if args.wait:
        state = _with_engine(lambda e: demo.wait(e, tenant, loaded.event_ids, args.wait))
        if state.finished and not state.failed:
            print(f"All {state.total} events reached a record.")
        else:
            print(
                f"{state.linked} of {state.total} events reached a record, {state.failed} "
                "failed. The worker may still be working: check `checkcord events failed`, "
                "`checkcord dlq list` and `docker compose logs worker`."
            )
    api = args.public_url or settings.public_base_url or "http://localhost:8000"
    print()
    print(demo.next_steps(loaded, api, settings.web_base_url))


def cmd_demo_remove(args: argparse.Namespace) -> None:
    from checkcord.services import demo

    tenant = _tenant(args)
    removed = _with_engine_and_redis(lambda e, r: demo.remove(e, r, tenant))
    print(
        f"Removed {removed.events} sample event(s), {removed.records} record(s), "
        f"{removed.people} people with {removed.keys} key(s), {removed.code_symbols} code "
        f"symbol(s) and {removed.audiences} audience(s). Audit entries are kept."
    )


def cmd_init(args: argparse.Namespace) -> None:
    from checkcord.services.keys import AdminKeyExistsError, init_admin_key

    tenant = _tenant(args)
    try:
        key_id, token = _with_engine(lambda e: init_admin_key(e, tenant, args.force))
    except AdminKeyExistsError:
        print(
            f"Tenant {tenant} already has an active admin key; nothing created. "
            "Use `checkcord keys list` to see it, or `checkcord init --force` to create another."
        )
        return
    _print_key(token, key_id, tenant, admin=True)


def cmd_keys_create(args: argparse.Namespace) -> None:
    from checkcord.services.keys import UserNotFoundError, create_key

    if args.create_user and not args.user:
        _fail("--create-user needs --user EMAIL")
    tenant = _tenant(args)
    try:
        key_id, token = _with_engine(
            lambda e: create_key(e, tenant, args.name, args.admin, args.user, args.create_user)
        )
    except UserNotFoundError as err:
        hint = "" if args.create_user else "; pass --create-user to add one"
        _fail(f"no active user {err} in tenant {tenant}{hint}")
    _print_key(token, key_id, tenant, admin=args.admin)
    if args.user:
        print(f"Bound to user {args.user.strip()}.")


def cmd_keys_list(args: argparse.Namespace) -> None:
    from checkcord.services.keys import list_keys

    tenant = _tenant(args)
    keys = _with_engine(lambda e: list_keys(e, tenant))
    if not keys:
        print(f"No API keys for tenant {tenant}.")
        return
    width = max([len("USER"), *(len(k.user_email or "-") for k in keys)])
    print(f"{'ID':36}  {'ADMIN':5}  {'STATUS':7}  {'CREATED':20}  {'USER':{width}}  NAME")
    for k in keys:
        status = "active" if k.active else "revoked"
        created = k.created_at.strftime("%Y-%m-%d %H:%M:%S")
        user = k.user_email or "-"
        print(
            f"{k.id!s:36}  {'yes' if k.is_admin else 'no':5}  {status:7}  {created:20}  "
            f"{user:{width}}  {k.name}"
        )


def cmd_keys_revoke(args: argparse.Namespace) -> None:
    from checkcord.services.keys import revoke_key

    tenant = _tenant(args)
    if not _with_engine(lambda e: revoke_key(e, tenant, args.key_id)):
        _fail(f"no API key {args.key_id} for tenant {tenant}")
    print(f"Revoked API key {args.key_id}. {_revocation_note()}")


def cmd_connect_oauth(args: argparse.Namespace) -> None:
    from checkcord import connectors
    from checkcord.services.connections import install_link

    tenant, base = _tenant(args), _public_url(args)
    settings = _settings()
    try:
        link = install_link(settings, args.provider, tenant, base)
    except ValueError as err:
        _fail(str(err))
    print(f"Install link for {link.name}, tenant {tenant}:")
    print()
    print(f"    {link.url}")
    print()
    print(
        f"Open it in a browser signed in as a {link.name} admin. It works once and expires in "
        f"{link.expires_in_seconds // 60} minutes; run this command again for a new one."
    )
    print(f"The {link.name} app must list this redirect URL: {link.redirect_uri}")
    connector = connectors.get(args.provider)
    if connector is not None and connector.oauth is not None and connector.oauth.notes:
        for line in connector.oauth.notes(settings):
            print(line)


def cmd_connect_registered(args: argparse.Namespace) -> None:
    from checkcord import connectors

    connector = connectors.get(args.connector)
    if connector is None or connector.setup is None:
        _fail(f"no connector {args.connector!r}")
    setup = connector.setup
    tenant, base = _tenant(args), _public_url(args)

    async def run(engine: Any) -> None:
        await setup.run(connectors.SetupContext(args, engine, tenant, base, args.review))

    try:
        _with_engine(run)
    except ValueError as err:
        _fail(str(err))
    if connector.audience_from == "admin":
        print(
            f"Nobody can read what {connector.name} ingests until you set an audience with "
            f"`checkcord connections audience {connector.name}:IDENTIFIER`."
        )


def _with_engine_and_redis[T](fn: Callable[[Any, Any], Awaitable[T]]) -> T:
    return _with_engine(lambda engine: _redis_run(lambda redis: fn(engine, redis)))


async def _redis_run[T](fn: Callable[[Any], Awaitable[T]]) -> T:
    from redis.asyncio import Redis

    redis = Redis.from_url(_settings().redis_url)
    try:
        return await fn(redis)
    finally:
        await redis.aclose()


def _print_counts(counts: dict[str, int]) -> None:
    for name, n in counts.items():
        print(f"  {name.replace('_', ' ') + ':':26} {n}")


def _confirm_purge(args: argparse.Namespace, tenant: UUID, selection: Any) -> bool:
    """Print what would be deleted; True when the caller should go ahead."""
    from checkcord.services import purge

    found = _with_engine(lambda e: purge.count(e, tenant, selection))
    print(f"Would delete from tenant {tenant}:")
    _print_counts(
        {
            "events": found.events,
            "records_deleted": found.records_deleted,
            "records_updated": found.records_updated,
            "meeting_segments": found.segments,
            "code_symbols": found.code_symbols,
        }
    )
    if getattr(args, "dry_run", False):
        return False
    if args.yes:
        return True
    try:
        answer = input("Delete this permanently? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        _fail("nothing deleted")
    return True


def _run_purge(tenant: UUID, selection: Any) -> None:
    from checkcord.services import purge
    from checkcord.storage import audit

    result = _with_engine_and_redis(
        lambda e, r: purge.purge(e, tenant, selection, actor=audit.CLI_ACTOR, redis=r)
    )
    print("Deleted:")
    _print_counts(result.counts())


def cmd_disconnect(args: argparse.Namespace) -> None:
    from checkcord.services import purge
    from checkcord.services.connections import disconnect

    tenant = _tenant(args)
    selection = purge.connection(args.provider, args.identifier)
    if args.purge:
        _confirm_purge(args, tenant, selection)
    if _with_engine(lambda e: disconnect(e, tenant, args.provider, args.identifier)):
        print(f"Disconnected {args.provider} {args.identifier} for tenant {tenant}.")
    elif not args.purge:
        _fail(f"no {args.provider} connection {args.identifier!r} for tenant {tenant}")
    if args.purge:
        _run_purge(tenant, selection)
        return
    left = _with_engine(lambda e: purge.count(e, tenant, selection))
    print(f"Content from it is still stored: {left.events} events, {left.records_updated} records.")
    if left.events:
        print(f"Delete it with: checkcord disconnect {args.provider} {args.identifier} --purge")


def cmd_purge_source(args: argparse.Namespace) -> None:
    from checkcord.services import purge

    tenant = _tenant(args)
    selection = purge.source(args.platform, args.container)
    if _confirm_purge(args, tenant, selection):
        _run_purge(tenant, selection)


def cmd_purge_event(args: argparse.Namespace) -> None:
    from checkcord.services import purge

    tenant = _tenant(args)
    selection = purge.events(args.ids)
    if _confirm_purge(args, tenant, selection):
        _run_purge(tenant, selection)


def cmd_purge_record(args: argparse.Namespace) -> None:
    from checkcord.services import purge

    tenant = _tenant(args)
    selection = purge.record(args.id)
    if _confirm_purge(args, tenant, selection):
        _run_purge(tenant, selection)


def cmd_removed_count(args: argparse.Namespace) -> None:
    from checkcord.services import purge

    tenant = _tenant(args)
    counts = _with_engine(lambda e: purge.removed_counts(e, tenant))
    print(f"Removed items remembered for tenant {tenant}:")
    if not counts:
        print("  none")
    _print_counts(counts)


def cmd_removed_clear(args: argparse.Namespace) -> None:
    from checkcord.services import purge
    from checkcord.storage import audit

    tenant = _tenant(args)
    counts = _with_engine(lambda e: purge.removed_counts(e, tenant))
    n = counts.get(args.platform, 0) if args.platform else sum(counts.values())
    which = f"{args.platform} " if args.platform else ""
    print(f"Would forget {n} removed {which}item(s) of tenant {tenant}.")
    if not args.yes:
        try:
            answer = input("Store them again when they are delivered again? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            _fail("nothing cleared")
    cleared = _with_engine(
        lambda e: purge.clear_removed(e, tenant, actor=audit.CLI_ACTOR, platform=args.platform)
    )
    print(f"Forgot {cleared} removed item(s).")


def cmd_users_delete(args: argparse.Namespace) -> None:
    from checkcord.services import purge
    from checkcord.storage import audit

    tenant = _tenant(args)
    found = _with_engine(lambda e: purge.find_person(e, tenant, email=args.email))
    if found is None:
        _fail(f"no user {args.email.strip()} in tenant {tenant}")
    print(f"Deletes user {args.email.strip()} ({found[0]}), their keys and the events they wrote.")
    if not _confirm_purge(args, tenant, found[1]):
        return
    deleted = _with_engine_and_redis(
        lambda e, r: purge.delete_person(
            e, tenant, actor=audit.CLI_ACTOR, user_id=found[0], redis=r
        )
    )
    if deleted is None:
        _fail(f"no user {args.email.strip()} in tenant {tenant}")
    print(f"Deleted user {deleted.user_id} and {deleted.keys} API key(s).")
    _print_counts(deleted.purged.counts())
    print(_revocation_note())


async def _all_pages(fetch: Callable[[Any], Awaitable[tuple[list[Any], Any]]]) -> list[Any]:
    rows: list[Any] = []
    cursor = None
    while True:
        page, cursor = await fetch(cursor)
        rows += page
        if cursor is None:
            return rows


def cmd_users_list(args: argparse.Namespace) -> None:
    from checkcord.services import people

    tenant = _tenant(args)
    active = None if args.all else True
    found = _with_engine(
        lambda e: _all_pages(
            lambda c: people.list_people(e, tenant, active=active, query=args.query, cursor=c)
        )
    )
    if not found:
        print(f"No people in tenant {tenant}.")
        return
    width = max(len("EMAIL"), *(len(p["email"]) for p in found))
    print(f"{'ID':36}  {'EMAIL':{width}}  {'ADMIN':5}  STATUS")
    for p in found:
        status = "active" if p["active"] else "inactive"
        admin = "yes" if p["is_admin"] else "no"
        print(f"{p['id']!s:36}  {p['email']:{width}}  {admin:5}  {status}")


def cmd_users_admin(args: argparse.Namespace) -> None:
    from checkcord.services import people
    from checkcord.storage import audit

    tenant = _tenant(args)
    on = args.state == "on"

    async def run(engine: Any) -> dict[str, Any] | None:
        user_id = await people.find_id(engine, tenant, args.email)
        if user_id is None:
            return None
        return await people.set_admin(engine, tenant, user_id, on, audit.CLI_ACTOR)

    found = _with_engine(run)
    if found is None:
        _fail(f"no single user {args.email.strip()} in tenant {tenant}")
    print(f"{found['email']} is {'now' if on else 'no longer'} an admin.")
    settings = _settings()
    if settings.scim_admin_groups:
        print(
            "SCIM_ADMIN_GROUPS is set, so their next sign-in or SCIM group change sets this again."
        )
    elif settings.oidc_admin_groups_claim:
        print("OIDC_ADMIN_GROUPS_CLAIM is set, so their next sign-in sets this from their groups.")


def cmd_sessions_list(args: argparse.Namespace) -> None:
    from checkcord.services import people, sessions

    tenant = _tenant(args)
    settings = _settings()

    async def run(engine: Any) -> list[dict[str, Any]] | None:
        user_id = None
        if args.user:
            user_id = await people.find_id(engine, tenant, args.user)
            if user_id is None:
                return None
        return await _all_pages(
            lambda c: sessions.list_sessions(
                engine, tenant, settings, user_id=user_id, include_ended=args.all, cursor=c
            )
        )

    found = _with_engine(run)
    if found is None:
        _fail(f"no single user {args.user.strip()} in tenant {tenant}")
    if not found:
        print(f"No {'' if args.all else 'live '}sessions in tenant {tenant}.")
        return
    width = max(len("USER"), *(len(s["user_email"]) for s in found))
    print(f"{'ID':36}  {'USER':{width}}  {'STATUS':7}  {'SIGNED IN':16}  LAST SEEN")
    for s in found:
        status = "live" if s["active"] else ("revoked" if s["revoked_at"] else "ended")
        print(
            f"{s['id']!s:36}  {s['user_email']:{width}}  {status:7}  "
            f"{s['created_at']:%Y-%m-%d %H:%M}  {s['last_seen_at']:%Y-%m-%d %H:%M}"
        )


def cmd_sessions_revoke(args: argparse.Namespace) -> None:
    from checkcord.services import sessions
    from checkcord.storage import audit

    tenant = _tenant(args)
    if not _with_engine(lambda e: sessions.revoke(e, tenant, args.session_id, audit.CLI_ACTOR)):
        _fail(f"no live session {args.session_id} in tenant {tenant}")
    print(f"Revoked session {args.session_id}. It stops working on its next request.")


def cmd_secrets_encrypt(args: argparse.Namespace) -> None:
    from checkcord.cli.secrets import encrypt_stored_secrets

    results = _with_engine(encrypt_stored_secrets)
    print(f"{'TABLE':32}  {'ROWS':>6}  {'ENCRYPTED':>9}")
    for r in results:
        print(f"{r.table:32}  {r.rows:>6}  {r.encrypted:>9}")
    print(f"Encrypted {sum(r.encrypted for r in results)} row(s); the rest were already encrypted.")


def _audience_text(setting: dict[str, Any] | None) -> str:
    if setting is None or setting["mode"] == "nobody":
        return "nobody"
    if setting["mode"] == "everyone":
        return "everyone"
    names = setting["groups"] if setting["mode"] == "groups" else setting["people"]
    text = f"{setting['mode']} {','.join(names)} ({setting['members']})"
    recompute = setting["recompute"]
    if recompute["state"] in ("pending", "running"):
        text += f" recomputing {recompute['done']}/{recompute['total']}"
    return text


def _audiences_by_connection(settings: list[dict[str, Any]]) -> dict[str, list[str]]:
    shown: dict[str, list[str]] = {}
    for s in settings:
        where = f"{s['container']}: " if s["container"] else ""
        shown.setdefault(s["connection"], []).append(where + _audience_text(s))
    return shown


def cmd_connections(args: argparse.Namespace) -> None:
    from checkcord.services import connection_audiences
    from checkcord.services.connections import connection_key, list_connections

    tenant = _tenant(args)

    async def run(engine: Any) -> tuple[list[Any], list[dict[str, Any]]]:
        return (
            await list_connections(engine, tenant),
            await connection_audiences.list_settings(engine, tenant),
        )

    rows, settings = _with_engine(run)
    if not rows:
        print(f"Nothing connected for tenant {tenant}.")
        return
    from checkcord import connectors

    admin = connection_audiences.admin_providers()
    shown = _audiences_by_connection(settings)
    width = max(30, *(len(c.identifier) for c in rows))
    print(
        f"{'PROVIDER':15}  {'STATUS':12}  {'CREATED':20}  {'STATE':6}  "
        f"{'IDENTIFIER':{width}}  AUDIENCE"
    )
    for c in rows:
        created = c.created_at.strftime("%Y-%m-%d %H:%M:%S") if c.created_at else "-"
        state = "review" if c.in_review else "live"
        status = connectors.status(c.provider) or "-"
        key = connection_key(c.provider, c.identifier)
        if c.provider in admin:
            audience = "; ".join(shown.get(key) or ["nobody"])
        else:
            found = connectors.get(c.provider)
            audience = f"from {found.audience_from}" if found else "-"
        print(
            f"{c.provider:15}  {status:12}  {created:20}  {state:6}  "
            f"{c.identifier:{width}}  {audience}"
        )


def cmd_connections_audience(args: argparse.Namespace) -> None:
    from checkcord.services import connection_audiences as ca
    from checkcord.storage import audit

    tenant, key = _tenant(args), _connection(args.connection)
    if args.everyone:
        mode, names = ca.EVERYONE, []
    elif args.group:
        mode, names = ca.GROUPS, args.group
    elif args.people:
        mode, names = ca.PEOPLE, args.people
    else:
        mode, names = ca.NOBODY, []

    def progress(done: int, total: int) -> None:
        print(f"  recomputed {done} of {total} event(s)", flush=True)

    async def run(engine: Any, redis: Any) -> tuple[Any, int]:
        setting = await ca.set_audience(
            engine,
            tenant,
            key,
            mode,
            audit.CLI_ACTOR,
            container=args.container,
            groups=names if mode == ca.GROUPS else (),
            people=names if mode == ca.PEOPLE else (),
        )
        print(f"Audience of {key}{' ' + setting.container if setting.container else ''} set.")
        changed = await ca.recompute(
            engine, tenant, key, setting.container, progress=progress, redis=redis
        )
        return setting, changed

    try:
        setting, changed = _with_engine_and_redis(run)
    except (ValueError, LookupError) as err:
        _fail(str(err))
    where = f" ({setting.container})" if setting.container else ""
    print(f"{key}{where}: {_audience_text({**setting.describe(), 'recompute': {'state': 'done'}})}")
    print(f"{changed} event(s) and the records based on them now have this audience.")


def _connection(value: str) -> str:
    provider, _, identifier = value.partition(":")
    if not provider or not identifier.strip():
        _fail(
            f"{value!r} is not a connection: use PROVIDER:IDENTIFIER from `checkcord connections`"
        )
    return f"{provider}:{identifier.strip()}"


def cmd_connections_review(args: argparse.Namespace) -> None:
    from checkcord.services import review
    from checkcord.storage import audit

    tenant, key = _tenant(args), _connection(args.connection)
    on = args.state == "on"
    _with_engine(lambda e: review.set_review(e, tenant, key, on, audit.CLI_ACTOR))
    if on:
        print(f"{key} is in review: what arrives from now on is held.")
    else:
        print(f"{key} is live: what arrives from now on is not held.")
        print(f"Held material stays held; run `checkcord review release {key}` or `reject`.")


def cmd_connections_scope(args: argparse.Namespace) -> None:
    from checkcord.services import scope
    from checkcord.storage import audit

    tenant, key = _tenant(args), _connection(args.connection)
    containers = args.containers
    if args.action in ("show", "clear") and containers:
        _fail(f"`{args.action}` takes no containers")
    if args.action in ("include", "exclude") and not containers:
        _fail(f"name at least one container to {args.action}")

    async def run(engine: Any) -> dict[str, Any]:
        if args.action != "show":
            current = await scope.rule(engine, tenant, key)
            if args.action == "clear":
                change: dict[str, list[str]] = {"include": [], "exclude": []}
            else:
                listed = list(getattr(current, args.action))
                names = [scope.normalize(c) for c in containers]
                if args.remove:
                    listed = [c for c in listed if c not in names]
                else:
                    listed += names
                change = {args.action: listed}
            await scope.change(engine, tenant, key, audit.CLI_ACTOR, **change)
        return await scope.show(engine, tenant, key)

    try:
        found = _with_engine(run)
    except (ValueError, LookupError) as err:
        _fail(str(err))
    print(f"Scope of {key}:")
    if found["include"]:
        print(f"  only:    {', '.join(found['include'])}")
    else:
        print("  include: everything")
    print(f"  exclude: {', '.join(found['exclude']) or '-'}")
    print(f"  skipped: {found['skipped']} event(s) in the last {found['days']} days")
    if args.action != "show":
        print("Applies to what arrives from now on. What was already ingested stays;")
        print("delete it with `checkcord purge source --platform P --container ID`.")


def cmd_review_list(args: argparse.Namespace) -> None:
    from checkcord.services import review
    from checkcord.storage import audit

    tenant = _tenant(args)
    connection = _connection(args.connection) if args.connection else None
    held = _with_engine(lambda e: review.list_held(e, tenant, audit.CLI_ACTOR, connection))
    if not held:
        print(f"No held records for tenant {tenant}.")
        return
    for r in held:
        sources = ", ".join(
            f"{s['platform']}:{s['container'] or '-'} ({s['events']})" for s in r["sources"]
        )
        print(f"{r['id']}  {r['connection']}  {r['context_type']}  {r['event_count']} event(s)")
        print(f"    sources: {sources or '-'}")
        print(f"    {r['summary'][:300]}")
    print(f"{len(held)} held record(s).")


def _review_target(args: argparse.Namespace) -> tuple[str | None, UUID | None]:
    if (args.connection is None) == (args.record is None):
        _fail("give a CONNECTION or --record ID")
    return (_connection(args.connection) if args.connection else None), args.record


def cmd_review_release(args: argparse.Namespace) -> None:
    from checkcord.services import review
    from checkcord.storage import audit

    tenant = _tenant(args)
    key, record = _review_target(args)
    if key is not None:
        released = _with_engine_and_redis(
            lambda e, r: review.release_connection(e, tenant, key, audit.CLI_ACTOR, r)
        )
        print(f"{key} is live.")
    else:
        assert record is not None
        found = _with_engine_and_redis(
            lambda e, r: review.release_record(e, tenant, record, audit.CLI_ACTOR, r)
        )
        if found is None:
            _fail(f"no held record {record}")
        released = found
    print(f"Released {len(released.records)} record(s) and {released.events} event(s).")


def cmd_review_reject(args: argparse.Namespace) -> None:
    from checkcord.services import purge, review
    from checkcord.storage import audit

    tenant = _tenant(args)
    key, record = _review_target(args)
    if key is not None:
        selection = purge.held(key)
    else:
        assert record is not None
        selection = purge.record(record)
    if not _confirm_purge(args, tenant, selection):
        return
    if key is not None:
        result = _with_engine_and_redis(
            lambda e, r: review.reject_connection(e, tenant, key, audit.CLI_ACTOR, r)
        )
    else:
        assert record is not None
        found = _with_engine_and_redis(
            lambda e, r: review.reject_record(e, tenant, record, audit.CLI_ACTOR, r)
        )
        if found is None:
            _fail(f"no held record {record}")
        result = found
    print("Deleted:")
    _print_counts(result.counts())


def _print_raw(tenant: UUID, record_id: UUID) -> None:
    from checkcord.services import records
    from checkcord.storage import audit

    found = _with_engine(lambda e: records.raw(e, tenant, record_id, audit.CLI_ACTOR))
    if found is None:
        _fail(f"no record {record_id}")
    print(f"id:   {found['id']}")
    print(f"type: {found['context_type']}")
    print("UNREDACTED; this read is audited.")
    print()
    print(found["summary"])
    for src in found["sources"]:
        print()
        print(f"source {src['id']}  {src['platform']}  {src['happened_at'] or '-'}")
        print(f"  {' '.join((src['text'] or '').split())}")
        for m in src.get("messages") or []:
            text = " ".join((m["text"] or "").split())
            print(f"  > {m['happened_at'] or '-'}  {m['author'] or '-'}: {text}")


def cmd_records_show(args: argparse.Namespace) -> None:
    from checkcord.services import records
    from checkcord.storage import audit

    tenant = _tenant(args)
    if args.raw:
        _print_raw(tenant, args.record_id)
        return
    found = _with_engine(lambda e: records.describe(e, tenant, args.record_id, audit.CLI_ACTOR))
    if found is None:
        _fail(f"no record {args.record_id} (held records are listed by `checkcord review list`)")
    print(f"id:         {found['id']}")
    print(f"type:       {found['context_type']}")
    print(f"severity:   {found['severity']}")
    print(f"scope:      {', '.join(found['scope'])}")
    print(f"audience:   {found['audience']}")
    print(f"confidence: {found['confidence_score']:.2f}")
    print(f"created:    {found['created_at']}")
    support = found["supporting_events"]
    print(f"events:     {support['count']} ({support['first'] or '-'} to {support['last'] or '-'})")
    if found["state"] == "retired":
        print(f"retired:    {found['retired_at']} by {found['retired_by']}")
        if found["replaced_by"]:
            print(f"replaced by {found['replaced_by']}")
        if found["retired_reason"]:
            print(f"reason:     {found['retired_reason']}")
    for suggested in found["suggested_replacements"]:
        print(
            f"suggested:  {suggested['replacement_id']} may replace it ({suggested['basis']}); "
            f"`checkcord records accept {suggested['id']}`"
        )
    note = found.get("may_be_replaced_by")
    if note:
        print(
            f"note:       a newer record from {(note['created_at'] or '')[:10]} may replace this; "
            f"an admin has not confirmed it yet: {' '.join(note['summary'].split())}"
        )
    print("sources:")
    for src in found["sources"]:
        print(f"  {src['platform']}:{src['container'] or '-'}  {src['events']} event(s)")
    print()
    print(found["summary"])
    if found["updates"]:
        print()
        print("updates:")
        for item in found["updates"]:
            source = item["source"]
            print(
                f"  {(item['happened_at'] or '-')[:10]}  {source['platform']} {source['type']}: "
                f"{' '.join(item['text'].split())}"
            )


_LIFECYCLE_ERRORS = {
    "not_found": "no record {id}",
    "retired": "record {id} is already retired",
    "not_retired": "record {id} is not retired",
    "self": "a record cannot replace itself",
    "replacement_not_found": "the replacement is not a live record",
}


def cmd_records_retire(args: argparse.Namespace) -> None:
    from checkcord.services import records
    from checkcord.storage import audit

    tenant = _tenant(args)
    error = _with_engine_and_redis(
        lambda e, r: records.retire(
            e,
            tenant,
            args.record_id,
            audit.CLI_ACTOR,
            replaced_by=args.replaced_by,
            reason=args.reason,
            redis=r,
        )
    )
    if error:
        _fail(_LIFECYCLE_ERRORS[error].format(id=args.record_id))
    print(
        f"Retired {args.record_id}: agents, context files and pull-request checks no longer see "
        f"it. `checkcord records restore {args.record_id}` brings it back."
    )


def cmd_records_restore(args: argparse.Namespace) -> None:
    from checkcord.services import records
    from checkcord.storage import audit

    tenant = _tenant(args)
    error = _with_engine_and_redis(
        lambda e, r: records.restore(e, tenant, args.record_id, audit.CLI_ACTOR, redis=r)
    )
    if error:
        _fail(_LIFECYCLE_ERRORS[error].format(id=args.record_id))
    print(f"Restored {args.record_id}.")


def cmd_records_suggestions(args: argparse.Namespace) -> None:
    from checkcord.services import records
    from checkcord.storage import audit

    tenant = _tenant(args)
    found, _ = _with_engine(lambda e: records.suggestions(e, tenant, audit.CLI_ACTOR, limit=200))
    if not found:
        print("No suggested replacements.")
        return
    for s in found:
        print(f"{s['id']}  ({s['basis']})")
        print(f"  old {s['record_id']}  {_clip(s['record_summary'])}")
        print(f"  new {s['replacement_id']}  {_clip(s['replacement_summary'])}")
    print()
    print("Nothing changes until you decide: `checkcord records accept|dismiss SUGGESTION_ID`.")


def _clip(text: str, width: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 3] + "..."


def cmd_records_decide(args: argparse.Namespace) -> None:
    from checkcord.services import records
    from checkcord.storage import audit

    tenant = _tenant(args)
    accept = args.records_command == "accept"
    error = _with_engine_and_redis(
        lambda e, r: records.decide_suggestion(
            e, tenant, args.suggestion_id, accept, audit.CLI_ACTOR, redis=r
        )
    )
    if error == "not_found":
        _fail(f"no open suggestion {args.suggestion_id}")
    if error:
        _fail(_LIFECYCLE_ERRORS[error].format(id=args.suggestion_id))
    print("Accepted: the older record is retired." if accept else "Dismissed.")


def cmd_records_severity(args: argparse.Namespace) -> None:
    from checkcord.services import records
    from checkcord.storage import audit

    tenant = _tenant(args)
    changed = _with_engine(
        lambda e: records.set_severity(e, tenant, args.record_id, args.severity, audit.CLI_ACTOR)
    )
    if not changed:
        _fail(f"no record {args.record_id}")
    effect = "fails" if args.severity == "blocking" else "no longer fails"
    print(f"Record {args.record_id} is {args.severity}: a pull request that touches it {effect}.")


def cmd_records_type(args: argparse.Namespace) -> None:
    from checkcord.services import records
    from checkcord.storage import audit

    tenant = _tenant(args)
    changed = _with_engine(
        lambda e: records.set_type(e, tenant, args.record_id, args.type, audit.CLI_ACTOR)
    )
    if not changed:
        _fail(f"no record {args.record_id}")
    kind = "a reference record" if args.type == "general" else "a decision"
    print(f"Record {args.record_id} is now {args.type}: {kind}.")


def cmd_audiences_list(args: argparse.Namespace) -> None:
    from datetime import UTC, datetime

    from checkcord.services import audiences

    tenant = _tenant(args)
    rows = _with_engine(lambda e: audiences.list_stored(e, tenant))
    if not rows:
        print(f"No stored audiences for tenant {tenant}.")
        return
    now = datetime.now(UTC)
    print(
        f"{'PLATFORM':8}  {'CONTAINER':40}  {'AUDIENCE':8}  {'MEMBERS':24}  {'REFRESHED':>9}  ERROR"
    )
    for r in rows:
        refreshed = (
            _age((now - r["refreshed_at"]).total_seconds()) if r["refreshed_at"] else "never"
        )
        print(
            f"{r['platform']:8}  {r['container'][:40]:40}  "
            f"{'public' if r['is_public'] else 'private':8}  {_matched(r):24}  "
            f"{refreshed:>9}  {(r['last_error'] or '-')[:100]}"
        )


def _matched(row: dict[str, Any]) -> str:
    if row["is_public"]:
        return "-"
    matched = len(row["member_ids"])
    return f"{matched} of {matched + (row.get('unmatched_count') or 0)} members matched"


def cmd_accounts_list(args: argparse.Namespace) -> None:
    from checkcord.services import accounts

    tenant = _tenant(args)
    try:
        rows = _with_engine(
            lambda e: _all_pages(
                lambda cursor: accounts.list_accounts(
                    e,
                    tenant,
                    platform=args.platform,
                    matched=False if args.unmatched else None,
                    cursor=cursor,
                )
            )
        )
    except ValueError as err:
        _fail(str(err))
    if not rows:
        print("No unmatched accounts." if args.unmatched else f"No accounts for tenant {tenant}.")
        return
    print(
        f"{'PLATFORM':8}  {'ACCOUNT':24}  {'NAME':20}  {'SOURCE EMAIL':28}  "
        f"{'PERSON':28}  {'LINK':7}  RESTRICTED"
    )
    for r in rows:
        print(
            f"{r['platform']:8}  {r['account'][:24]:24}  {(r['display_name'] or '-')[:20]:20}  "
            f"{(r['source_email'] or '-')[:28]:28}  {(r['person_email'] or '-')[:28]:28}  "
            f"{(r['link_source'] or '-'):7}  {r['unmatched_in'] or '-'}"
        )


def cmd_accounts_link(args: argparse.Namespace) -> None:
    from checkcord.services import accounts
    from checkcord.storage import audit

    tenant = _tenant(args)
    try:
        done = _with_engine_and_redis(
            lambda e, r: accounts.link(
                e,
                tenant,
                args.platform,
                args.account,
                actor=audit.CLI_ACTOR,
                email=args.email,
                redis=r,
            )
        )
    except (ValueError, LookupError) as err:
        _fail(str(err))
    print(f"{done.platform}:{done.account} is matched to {done.person_email}.")


def cmd_accounts_unlink(args: argparse.Namespace) -> None:
    from checkcord.services import accounts
    from checkcord.storage import audit

    tenant = _tenant(args)
    try:
        done = _with_engine_and_redis(
            lambda e, r: accounts.unlink(
                e, tenant, args.platform, args.account, actor=audit.CLI_ACTOR, redis=r
            )
        )
    except ValueError as err:
        _fail(str(err))
    if done is None:
        _fail(f"no account {args.platform}:{args.account}")
    print(f"{done.platform}:{done.account} is not matched to a person; a sync will not match it.")


def cmd_accounts_import(args: argparse.Namespace) -> None:
    from checkcord.services import accounts
    from checkcord.storage import audit

    tenant = _tenant(args)
    try:
        with open(args.file, newline="", encoding="utf-8") as f:
            rows = accounts.read_csv(f)
    except OSError as err:
        _fail(f"cannot read {args.file}: {err}")
    except ValueError as err:
        _fail(str(err))
    result = _with_engine_and_redis(
        lambda e, r: accounts.import_links(e, tenant, rows, actor=audit.CLI_ACTOR, redis=r)
    )
    print(f"linked:          {result.linked}")
    print(f"unknown account: {result.unknown_accounts} (created, so the link holds before a sync)")
    print(f"unknown person:  {result.unknown_people}")
    for line in result.rejected:
        print(f"  skipped {line}")


def cmd_audiences_refresh(args: argparse.Namespace) -> None:
    from checkcord.services import audiences
    from checkcord.storage import audit

    tenant = _tenant(args)
    if not args.all and not (args.platform and args.container):
        _fail("pass --platform and --container, or --all")
    platform, container = (None, None) if args.all else (args.platform, args.container)
    outcomes = _with_engine_and_redis(
        lambda e, r: audiences.refresh_selected(
            e, tenant, platform, container, trigger="cli", redis=r, actor=audit.CLI_ACTOR
        )
    )
    if not outcomes:
        print(f"No stored audiences for tenant {tenant}.")
    failed = False
    for o in outcomes:
        a = o.applied
        audience = "public" if a.after.is_public else f"private, {len(a.after.member_ids)} members"
        state = "kept last known audience" if a.kept else audience
        print(
            f"{o.platform}:{o.container}: {state}; {a.events} events, {a.records} records updated"
            + (f"; read failed: {o.error}" if o.error else "")
        )
        failed = failed or o.error is not None
    if failed:
        sys.exit(1)


def cmd_events_failed(args: argparse.Namespace) -> None:
    from datetime import UTC, datetime

    from checkcord.services import events
    from checkcord.storage import redis_trees

    tenant = _tenant(args)
    failed, retrying = _with_engine(lambda e: events.list_failed(e, tenant, limit=args.limit))
    parked = _with_redis(lambda r: redis_trees.failed_discussions(r, tenant))
    now = datetime.now(UTC)
    if not failed:
        print(f"No failed events for tenant {tenant}.")
    else:
        print(f"{'ID':36}  {'SOURCE':28}  {'AGE':>5}  {'TRIES':>5}  LAST ERROR")
        for e in failed:
            age = _age((now - e.ingested_at).total_seconds())
            print(
                f"{e.event_id!s:36}  {e.source:28}  {age:>5}  {e.attempts:>5}  "
                f"{(e.last_error or '-')[:100]}"
            )
    if retrying:
        print(f"{retrying} more event(s) are still being retried.")
    if not parked:
        print(f"No failed discussions for tenant {tenant}.")
    else:
        print()
        print("Failed discussions (extraction stopped after repeated unusable replies):")
        print(f"{'ID':36}  {'CHANNEL':28}  {'MESSAGES':>8}  {'TRIES':>5}  LAST ERROR")
        for t in parked:
            channel = f"{t.get('platform')}:{t.get('channel_name') or t.get('channel_id')}"
            print(
                f"{t['tree_id']:36}  {channel[:28]:28}  {t.get('message_count', '0'):>8}  "
                f"{t.get('attempts', '0'):>5}  {(t.get('last_error') or '-')[:100]}"
            )
    if failed or parked:
        print("Retry them with `checkcord events retry --all` or `--id ID`.")


def cmd_events_retry(args: argparse.Namespace) -> None:
    from checkcord.services import events
    from checkcord.storage import audit, redis_trees

    tenant = _tenant(args)
    retried = _with_engine(
        lambda e: events.retry(e, tenant, None if args.all else args.ids, audit.CLI_ACTOR)
    )
    rest = None if args.all else [str(i) for i in set(args.ids or []) - set(retried)]

    async def requeue(redis: Any) -> tuple[int, list[str]]:
        parked = {t["tree_id"] for t in await redis_trees.failed_discussions(redis, tenant)}
        missing = sorted(set(rest or []) - parked)
        return await redis_trees.retry_discussions(redis, tenant, rest), missing

    queued, missing = _with_redis(requeue)
    if queued:
        _record(tenant, "discussions.retried", params={"all": bool(args.all)}, result_count=queued)
    for item in missing:
        print(f"{item}: not a failed event or discussion of tenant {tenant}; skipped")
    print(
        f"Queued {len(retried)} event(s) and {queued} discussion(s); "
        "the worker retries them within a minute or so."
    )
    if missing:
        sys.exit(1)


def _dlq_call[T](fn: Callable[[Any], Awaitable[T]]) -> T:
    from redis.exceptions import ResponseError

    try:
        return _with_redis(fn)
    except ValueError as e:
        _fail(str(e))
    except ResponseError as e:
        _fail(f"redis refused the request: {e}")


def cmd_dlq_list(args: argparse.Namespace) -> None:
    from checkcord.bus.dead_letter import entry_age_seconds, list_streams

    streams = _dlq_call(list_streams)
    if not streams:
        print("No dead-letter streams.")
        return
    print(f"{'STREAM':40}  {'ENTRIES':>7}  OLDEST")
    for st in streams:
        print(f"{st.name:40}  {st.entries:>7}  {_age(entry_age_seconds(st.oldest_id))}")


def cmd_dlq_show(args: argparse.Namespace) -> None:
    from checkcord.bus.dead_letter import entries, entry_age_seconds, last_error_line
    from checkcord.core.secret_patterns import redact_secrets

    letters = _dlq_call(lambda r: entries(r, args.stream, limit=args.limit))
    if not letters:
        print(f"{args.stream} is empty.")
        return
    print(f"{'ID':17}  {'AGE':>5}  {'EVENT':36}  ERROR")
    for letter in letters:
        event_id = letter.fields.get("event_id", "-")
        error = redact_secrets(last_error_line(letter.error))[:120] or "-"
        print(
            f"{letter.entry_id:17}  {_age(entry_age_seconds(letter.entry_id)):>5}  "
            f"{event_id:36}  {error}"
        )
    print("Message content and credentials are not shown.")


def cmd_dlq_replay(args: argparse.Namespace) -> None:
    from checkcord.bus.dead_letter import replay, source_stream

    tenant = _tenant(args)
    done = _dlq_call(lambda r: replay(r, args.stream, None if args.all else args.ids))
    _record_dlq(tenant, "dlq.replay", args, done)
    print(f"Replayed {len(done)} {_entries(len(done))} to {source_stream(args.stream)}.")
    _report_missing(args, done)


def cmd_dlq_drop(args: argparse.Namespace) -> None:
    from checkcord.bus.dead_letter import drop

    tenant = _tenant(args)
    done = _dlq_call(lambda r: drop(r, args.stream, None if args.all else args.ids))
    _record_dlq(tenant, "dlq.drop", args, done)
    print(f"Dropped {len(done)} {_entries(len(done))} from {args.stream}.")
    _report_missing(args, done)


def _record_dlq(tenant: UUID, action: str, args: argparse.Namespace, done: list[str]) -> None:
    # Dead-letter streams are shared by all tenants; the entry goes to the tenant named here.
    _record(
        tenant,
        action,
        params={"stream": args.stream, "all": args.all},
        record_ids=done,
        result_count=len(done),
    )


def _audit_filters(args: argparse.Namespace) -> Any:
    from checkcord.storage.audit import Filters

    return Filters(
        since=args.since,
        until=args.until,
        user=getattr(args, "user", None),
        key=getattr(args, "key", None),
        action=getattr(args, "action", None),
        channel=getattr(args, "channel", None),
        record_id=getattr(args, "record", None),
    )


def cmd_audit_list(args: argparse.Namespace) -> None:
    from checkcord.storage import audit
    from checkcord.storage.postgres import tenant_context

    tenant = _tenant(args)
    filters = _audit_filters(args)

    async def run(engine: Any) -> tuple[list[dict[str, Any]], str | None]:
        async with tenant_context(engine, tenant) as conn:
            rows, next_cursor = await audit.list_entries(
                conn, tenant, filters, limit=args.limit, cursor=args.cursor
            )
            await audit.record(
                conn,
                tenant,
                audit.CLI_ACTOR,
                "audit.read",
                params={**filters.params(), "cursor": args.cursor, "limit": args.limit},
                result_count=len(rows),
            )
        return rows, next_cursor

    try:
        rows, next_cursor = _with_engine(run)
    except ValueError as e:
        _fail(str(e))
    if not rows:
        print(f"No audit entries for tenant {tenant}.")
        return
    print(f"{'TIME':19}  {'CHANNEL':7}  {'ACTION':32}  {'RECORDS':>7}  WHO")
    for r in rows:
        who = r["user_email"] or r["key_name"] or "-"
        print(
            f"{r['created_at'].strftime('%Y-%m-%d %H:%M:%S'):19}  {r['channel']:7}  "
            f"{r['action']:32}  {len(r['record_ids']):>7}  {who}"
        )
    if next_cursor:
        print(f"More entries: pass --cursor {next_cursor}")


def cmd_audit_export(args: argparse.Namespace) -> None:
    import csv
    import json

    from checkcord.storage import audit
    from checkcord.storage.postgres import tenant_context

    tenant = _tenant(args)
    filters = _audit_filters(args)
    _record(tenant, "audit.export", params={**filters.params(), "format": args.format})

    async def run(engine: Any, out: Any) -> int:
        writer = csv.writer(out)
        if args.format == "csv":
            writer.writerow(audit.COLUMNS)
        count = 0
        async with tenant_context(engine, tenant) as conn:
            async for row in audit.stream_entries(conn, tenant, filters):
                if args.format == "csv":
                    writer.writerow(audit.csv_row(row))
                else:
                    out.write(json.dumps(audit.jsonable(row)) + "\n")
                count += 1
        return count

    if not args.output:
        _with_engine(lambda e: run(e, sys.stdout))
        return
    with open(args.output, "w", newline="") as out:
        count = _with_engine(lambda e: run(e, out))
    print(f"Wrote {count} audit {_entries(count)} to {args.output}.")


def _entries(n: int) -> str:
    return "entry" if n == 1 else "entries"


def _report_missing(args: argparse.Namespace, done: list[str]) -> None:
    missing = [i for i in args.ids or [] if i not in done]
    for entry_id in missing:
        print(f"{entry_id}: no such entry in {args.stream}")
    if missing:
        sys.exit(1)


def cmd_audit_prune(args: argparse.Namespace) -> None:
    from checkcord.services import purge
    from checkcord.storage.postgres import create_checkcord_engine

    settings = _settings()
    days = args.days or settings.audit_retention_days
    if days is None:
        _fail("no retention: pass --days N or set AUDIT_RETENTION_DAYS")
    if days < 1:
        _fail("--days must be at least 1")

    async def run(dry_run: bool) -> int:
        # The app role cannot delete audit entries; only the schema owner can.
        engine = create_checkcord_engine(settings.effective_migration_database_url)
        try:
            return await purge.prune_audit(engine, days, dry_run=dry_run)
        finally:
            await engine.dispose()

    n = asyncio.run(run(True))
    print(f"{n} audit entries are older than {days} days.")
    if args.dry_run or not n:
        return
    if not args.yes:
        try:
            answer = input("Delete them permanently? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            _fail("nothing deleted")
    print(f"Deleted {asyncio.run(run(False))} audit entries.")


def cmd_redaction_recompute(args: argparse.Namespace) -> None:
    from checkcord.llm import usage
    from checkcord.pipeline import redaction
    from checkcord.storage.postgres import list_tenant_ids

    version = redaction.current_version()

    async def run(engine: Any) -> tuple[int, int, int]:
        tenants = [args.tenant] if args.tenant else await list_tenant_ids(engine)
        counts = {"records": 0, "updates": 0}
        pending = 0
        try:
            async for step in redaction.recompute(engine, tenants, batch_size=args.batch_size):
                counts[step.kind] += step.updated
                pending += step.pending
                print(
                    f"{step.tenant_id}  {counts[step.kind]} {step.kind[:-1]}(s) done, "
                    f"up to {step.last_id}"
                )
        finally:
            await usage.flush(engine)
        return counts["records"], counts["updates"], pending

    updated, updates, pending = _with_engine(run)
    print(f"Stored redaction for {updated} record(s) and {updates} update(s) at version {version}.")
    if pending:
        print(f"{pending} of them got the regex pass only; the worker retries the model pass.")


def _tokens(n: int) -> str:
    return f"{n:,}"


def cmd_usage(args: argparse.Namespace) -> None:
    from checkcord.llm import usage as counters
    from checkcord.services import usage
    from checkcord.storage import audit

    tenant = _tenant(args)

    async def run(engine: Any) -> dict[str, Any]:
        await counters.flush(engine)
        return await usage.report(engine, tenant, args.days, audit.CLI_ACTOR)

    found = _with_engine(run)
    if not found["rows"]:
        print(f"No model calls for tenant {tenant} since {found['since']}.")
        return
    priced = found["totals"]["estimated_cost"] is not None
    print(f"Model usage for tenant {tenant} since {found['since']}:")
    head = f"{'KIND':9}  {'PURPOSE':22}  {'MODEL':28}  {'CALLS':>7}  {'FAILED':>6}"
    print(head + f"  {'INPUT TOK':>12}  {'OUTPUT TOK':>12}" + ("  EST. COST" if priced else ""))
    for r in found["rows"]:
        line = (
            f"{r['kind']:9}  {r['purpose'][:22]:22}  {r['model'][:28]:28}  {r['calls']:>7}  "
            f"{r['failures']:>6}  {_tokens(r['input_tokens']):>12}  "
            f"{_tokens(r['output_tokens']):>12}"
        )
        if priced:
            cost = r["estimated_cost"]
            line += f"  {'-' if cost is None else f'${cost:.4f}'}"
        print(line)
    t = found["totals"]
    print(
        f"Total: {t['calls']} call(s), {t['failures']} failed, {_tokens(t['input_tokens'])} input "
        f"and {_tokens(t['output_tokens'])} output tokens"
        + (f", about ${t['estimated_cost']:.4f}" if priced else "")
        + "."
    )
    if not priced:
        print("Set MODEL_PRICES to see cost estimates.")


def cmd_stats(args: argparse.Namespace) -> None:
    from redis.asyncio import Redis

    from checkcord.services import stats
    from checkcord.storage import audit

    tenant = _tenant(args)

    async def run(engine: Any) -> dict[str, Any]:
        redis = Redis.from_url(_settings().redis_url)
        try:
            return await stats.summary(engine, tenant, args.days, audit.CLI_ACTOR, redis)
        finally:
            await redis.aclose()

    found = _with_engine(run)
    print(f"Activity for tenant {tenant} since {found['since']}")
    print()
    print("Stored by arrival day (chat messages kept, other events):")
    if not found["events"]:
        print("  none")
    for e in found["events"]:
        print(
            f"  {e['day']:10}  {e['source'] or 'unknown':20}  {e['messages']:>7} messages"
            f"  {e['events']:>7} events"
        )
    print("Records:")
    if not found["records"]:
        print("  none")
    for kind, title in (("decision", "Decisions"), ("reference", "Reference")):
        rows = [r for r in found["records"] if r.get("kind") == kind]
        if not rows:
            continue
        print(f"  {title}:")
        for r in rows:
            print(
                f"    {r['type']:20}  {r['live']:>6} live  {r['held']:>6} held"
                f"  {r['retired']:>6} retired"
            )
    print(f"Records with removed instructions: {found['records_with_removed_instructions']}")
    print(f"Removed items remembered: {found['removed_remembered']}")
    u = found["events_unfinished"]
    print(f"Events unfinished: {u['pending']} pending, {u['failed']} failed")
    d = found.get("discussions")
    if d is None:
        print("Discussions: redis not reachable")
    else:
        print(
            f"Chat messages waiting to be grouped: {d['buffered']} "
            f"(oldest {d['buffered_oldest_minutes']:g} min)"
        )
        print(f"Discussions waiting: {d['waiting']} (oldest {d['oldest_minutes']:g} min)")
        print(f"Discussions failed: {d['failed']}")
        print(f"Discussions with no outcome: {d['no_outcome']}")
        print(f"Outcomes dropped as not settled: {d.get('not_settled', 0)}")
    dl = found["dead_letters"]
    if dl is None:
        print("Dead letters: redis not reachable")
    else:
        print(f"Dead letters: {sum(dl.values())}")
        for name, n in dl.items():
            print(f"  {name:40}  {n:>7}")
    print(f"Agent reads ({found['people_reading']} distinct people):")
    if not found["agent_reads"]:
        print("  none")
    for r in found["agent_reads"]:
        print(f"  {r['day']:10}  {r['reads']:>7} reads  {r['people']:>4} people")
    c = found["pr_checks"]
    print(
        f"Pull-request checks: {c['posted']} posted, {c['conflicts']} with conflicts, "
        f"{c['blocked']} blocked, {c['errors']} errors"
    )
    print(f"Pull-request checks skipped (Checkcord's own branches): {c['skipped']}")
    print(f"Events skipped by connection scope: {found['scope_skipped']}")
    print("Connections:")
    if not found["connections"]:
        print("  none")
    for conn in found["connections"]:
        last = conn["last_event_at"] or "-"
        skipped = f"  {conn['scope_skipped']} skipped" if conn.get("scope_skipped") else ""
        print(f"  {conn['connection']:40}  {conn['state']:6}  last event {last}{skipped}")


def cmd_doctor(args: argparse.Namespace) -> None:
    from checkcord.services.doctor import run_checks

    checks = asyncio.run(run_checks(_settings()))
    for c in checks:
        print(f"{c.status}  {c.name:18} {c.detail}")
    if not all(c.ok for c in checks):
        sys.exit(1)


# Parser


def _add_tenant(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--tenant", type=UUID, metavar="UUID", help="tenant id (default: DEFAULT_TENANT_ID)"
    )


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"{value} is not a positive number")
    return n


def _add_yes(p: argparse.ArgumentParser) -> None:
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")


def _add_purge_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dry-run", action="store_true", help="print the counts only")
    _add_yes(p)
    _add_tenant(p)


def _timestamp(value: str) -> Any:
    from datetime import UTC, datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 time: {value!r}") from err
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _add_time_range(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--since", type=_timestamp, metavar="TIME", help="from this time (ISO 8601, UTC)"
    )
    p.add_argument("--until", type=_timestamp, metavar="TIME", help="before this time")


def _add_public_url(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--public-url",
        metavar="URL",
        help="public base URL of this API, used for webhook URLs (default: PUBLIC_BASE_URL)",
    )


def _add_review(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--review",
        action="store_true",
        help="hold what arrives until an admin releases it (`checkcord review`)",
    )


def _labelled(provider: str, text: str) -> str:
    from checkcord import connectors

    status = connectors.status(provider)
    return f"{text} [{status}]" if status else text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="checkcord",
        description="Checkcord: run, operate and connect a self-hosted deployment.",
    )
    from checkcord.core.version import checkcord_version

    parser.add_argument("--version", action="version", version=f"checkcord {checkcord_version()}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    p = sub.add_parser("api", help="run the HTTP API")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000, help="port (default: 8000)")
    p.add_argument(
        "--forwarded-allow-ips",
        metavar="IPS",
        help="proxies trusted for X-Forwarded-* headers (default: FORWARDED_ALLOW_IPS)",
    )
    p.set_defaults(handler=cmd_api)

    p = sub.add_parser("worker", help="run the pipeline consumers")
    p.add_argument(
        "--heartbeat-file", type=Path, metavar="PATH", help="touch this file while running"
    )
    p.set_defaults(handler=cmd_worker)

    p = sub.add_parser(
        "mcp",
        help="serve the MCP tools over stdio, forwarding each call to CHECKCORD_PROXY_API_URL "
        "with CHECKCORD_PROXY_API_KEY",
    )
    p.set_defaults(handler=cmd_mcp)

    p = sub.add_parser(
        "migrate",
        help="apply database migrations and size the embedding column to EMBEDDING_DIMENSION",
    )
    p.add_argument(
        "--reset-embeddings",
        action="store_true",
        help="clear stored embeddings when the column must change width",
    )
    p.set_defaults(handler=cmd_migrate)

    p = sub.add_parser("init", help="create the first admin API key")
    _add_tenant(p)
    p.add_argument("--force", action="store_true", help="create one even if an admin key exists")
    p.set_defaults(handler=cmd_init)

    keys = sub.add_parser("keys", help="manage API keys")
    keys_sub = keys.add_subparsers(dest="keys_command", metavar="COMMAND", required=True)
    p = keys_sub.add_parser("create", help="create an API key")
    p.add_argument("--name", required=True, help="label for the key")
    p.add_argument("--admin", action="store_true", help="allow admin endpoints")
    p.add_argument("--user", metavar="EMAIL", help="bind the key to this person")
    p.add_argument("--create-user", action="store_true", help="create the --user person if missing")
    _add_tenant(p)
    p.set_defaults(handler=cmd_keys_create)
    p = keys_sub.add_parser("list", help="list API keys (never shows the key itself)")
    _add_tenant(p)
    p.set_defaults(handler=cmd_keys_list)
    p = keys_sub.add_parser("revoke", help="revoke an API key")
    p.add_argument("key_id", type=UUID, metavar="KEY_ID", help="id from `checkcord keys list`")
    _add_tenant(p)
    p.set_defaults(handler=cmd_keys_revoke)

    connect = sub.add_parser("connect", help="connect a source and print its webhook setup")
    connect_sub = connect.add_subparsers(dest="provider", metavar="PROVIDER", required=True)

    from checkcord import connectors
    from checkcord.services.connections import providers

    for c in connectors.connectors():
        if c.setup is not None:
            p = connect_sub.add_parser(c.name, help=_labelled(c.name, c.setup.help))
            if c.setup.arguments is not None:
                c.setup.arguments(p)
            _add_review(p)
            _add_tenant(p)
            _add_public_url(p)
            p.set_defaults(handler=cmd_connect_registered, connector=c.name)
        elif c.oauth is not None:
            p = connect_sub.add_parser(
                c.name, help=_labelled(c.name, f"print a one-time {c.title} install link")
            )
            _add_tenant(p)
            _add_public_url(p)
            p.set_defaults(handler=cmd_connect_oauth)

    stored = providers()

    p = sub.add_parser("disconnect", help="remove a connected source")
    p.add_argument("provider", choices=stored, metavar="PROVIDER", help=", ".join(stored))
    p.add_argument("identifier", help="the IDENTIFIER shown by `checkcord connections`")
    p.add_argument("--purge", action="store_true", help="also delete the content ingested from it")
    _add_yes(p)
    _add_tenant(p)
    p.set_defaults(handler=cmd_disconnect)

    from checkcord.models.enums import SourcePlatform

    purge_p = sub.add_parser("purge", help="delete stored content and what was made from it")
    purge_sub = purge_p.add_subparsers(dest="purge_command", metavar="COMMAND", required=True)
    p = purge_sub.add_parser("source", help="every event of one channel, repository or space")
    p.add_argument("--platform", required=True, choices=[sp.value for sp in SourcePlatform])
    p.add_argument("--container", required=True, metavar="ID", help="channel id or OWNER/NAME")
    _add_purge_flags(p)
    p.set_defaults(handler=cmd_purge_source)
    p = purge_sub.add_parser("event", help="one or more events")
    p.add_argument("--id", dest="ids", type=UUID, action="append", required=True, metavar="UUID")
    _add_purge_flags(p)
    p.set_defaults(handler=cmd_purge_event)
    p = purge_sub.add_parser("record", help="a record and the events linked only to it")
    p.add_argument("--id", type=UUID, required=True, metavar="UUID")
    _add_purge_flags(p)
    p.set_defaults(handler=cmd_purge_record)

    removed_p = sub.add_parser("removed", help="items removed by an admin, kept out of ingestion")
    removed_sub = removed_p.add_subparsers(dest="removed_command", metavar="COMMAND", required=True)
    p = removed_sub.add_parser("count", help="how many removed items are remembered per platform")
    _add_tenant(p)
    p.set_defaults(handler=cmd_removed_count)
    p = removed_sub.add_parser("clear", help="forget removed items so they can be stored again")
    p.add_argument("--platform", choices=[sp.value for sp in SourcePlatform])
    _add_yes(p)
    _add_tenant(p)
    p.set_defaults(handler=cmd_removed_clear)

    users_p = sub.add_parser("users", help="manage people")
    users_sub = users_p.add_subparsers(dest="users_command", metavar="COMMAND", required=True)
    p = users_sub.add_parser("delete", help="delete a person, their keys and what they wrote")
    p.add_argument("email")
    _add_purge_flags(p)
    p.set_defaults(handler=cmd_users_delete)
    p = users_sub.add_parser("list", help="list people with their admin flag")
    p.add_argument("--all", action="store_true", help="include deactivated people")
    p.add_argument("--query", metavar="TEXT", help="part of the email or name")
    _add_tenant(p)
    p.set_defaults(handler=cmd_users_list)
    p = users_sub.add_parser("admin", help="give or take a person's admin rights for sign-in")
    p.add_argument("email")
    p.add_argument("state", choices=["on", "off"])
    _add_tenant(p)
    p.set_defaults(handler=cmd_users_admin)

    sess = sub.add_parser("sessions", help="browser sign-in sessions")
    sess_sub = sess.add_subparsers(dest="sessions_command", metavar="COMMAND", required=True)
    p = sess_sub.add_parser("list", help="list live sessions, newest first")
    p.add_argument("--user", metavar="EMAIL", help="only this person's sessions")
    p.add_argument("--all", action="store_true", help="include revoked and expired sessions")
    _add_tenant(p)
    p.set_defaults(handler=cmd_sessions_list)
    p = sess_sub.add_parser("revoke", help="end a session at once")
    p.add_argument("session_id", type=UUID, metavar="SESSION_ID")
    _add_tenant(p)
    p.set_defaults(handler=cmd_sessions_revoke)

    secrets_p = sub.add_parser("secrets", help="manage stored connector credentials")
    secrets_sub = secrets_p.add_subparsers(dest="secrets_command", metavar="COMMAND", required=True)
    p = secrets_sub.add_parser("encrypt", help="encrypt credentials stored in plaintext")
    p.set_defaults(handler=cmd_secrets_encrypt)

    conns = sub.add_parser("connections", help="list connected sources (no secrets)")
    _add_tenant(conns)
    conns.set_defaults(handler=cmd_connections)
    conns_sub = conns.add_subparsers(dest="connections_command", metavar="COMMAND")
    p = conns_sub.add_parser("review", help="hold a connection's new material for review, or not")
    p.add_argument("connection", metavar="CONNECTION", help="PROVIDER:IDENTIFIER")
    p.add_argument("state", choices=("on", "off"))
    _add_tenant(p)
    p.set_defaults(handler=cmd_connections_review)
    p = conns_sub.add_parser(
        "scope",
        help="show or change which channels, repositories, projects or pages are ingested",
        description="Exclude wins. A non-empty include list means only what it lists. "
        "Container ids are matched without regard to case.",
    )
    p.add_argument("connection", metavar="CONNECTION", help="PROVIDER:IDENTIFIER")
    p.add_argument("action", choices=("show", "include", "exclude", "clear"))
    p.add_argument("containers", nargs="*", metavar="CONTAINER")
    p.add_argument("--remove", action="store_true", help="take these off the list instead")
    _add_tenant(p)
    p.set_defaults(handler=cmd_connections_scope)
    p = conns_sub.add_parser(
        "audience",
        help="set who may read what a Jira, Notion, Linear, Sentry or Datadog connection ingests",
        description="The setting for a container wins over the connection's. What was "
        "already ingested is brought in line before the command returns.",
    )
    p.add_argument("connection", metavar="CONNECTION", help="PROVIDER:IDENTIFIER")
    p.add_argument(
        "--container", metavar="C", help="only this Jira project, Linear team or Notion parent"
    )
    who = p.add_mutually_exclusive_group(required=True)
    who.add_argument("--everyone", action="store_true", help="everyone in the tenant")
    who.add_argument("--group", nargs="+", metavar="NAME", help="members of these SCIM groups")
    who.add_argument("--people", nargs="+", metavar="EMAIL", help="these people")
    who.add_argument("--nobody", action="store_true", help="no one (the default)")
    _add_tenant(p)
    p.set_defaults(handler=cmd_connections_audience)

    acc = sub.add_parser("accounts", help="Slack, Teams and GitHub accounts and their people")
    acc_sub = acc.add_subparsers(dest="accounts_command", metavar="COMMAND", required=True)
    p = acc_sub.add_parser("list", help="list accounts and the person each is matched to")
    p.add_argument("--platform", metavar="P", help="slack, teams or github")
    p.add_argument("--unmatched", action="store_true", help="only accounts with no person")
    _add_tenant(p)
    p.set_defaults(handler=cmd_accounts_list)
    p = acc_sub.add_parser(
        "link", help="match an account to a person by hand; a sync never changes it"
    )
    p.add_argument("platform", metavar="PLATFORM", help="slack, teams or github")
    p.add_argument("account", metavar="ACCOUNT", help="user id, or GitHub login")
    p.add_argument("email", metavar="EMAIL", help="the person's email")
    _add_tenant(p)
    p.set_defaults(handler=cmd_accounts_link)
    p = acc_sub.add_parser("unlink", help="take an account's person away; a sync will not match it")
    p.add_argument("platform", metavar="PLATFORM", help="slack, teams or github")
    p.add_argument("account", metavar="ACCOUNT", help="user id, or GitHub login")
    _add_tenant(p)
    p.set_defaults(handler=cmd_accounts_unlink)
    p = acc_sub.add_parser("import", help="link accounts from a CSV file")
    p.add_argument("file", metavar="FILE", help="CSV with the header platform,account,email")
    _add_tenant(p)
    p.set_defaults(handler=cmd_accounts_import)

    rev = sub.add_parser("review", help="list, release or reject material held for review")
    rev_sub = rev.add_subparsers(dest="review_command", metavar="COMMAND", required=True)
    p = rev_sub.add_parser("list", help="list held records (redacted)")
    p.add_argument("--connection", metavar="CONNECTION", help="only this PROVIDER:IDENTIFIER")
    _add_tenant(p)
    p.set_defaults(handler=cmd_review_list)
    p = rev_sub.add_parser("release", help="make held material readable; a connection goes live")
    p.add_argument("connection", nargs="?", metavar="CONNECTION", help="PROVIDER:IDENTIFIER")
    p.add_argument(
        "--record", type=UUID, metavar="UUID", help="one record and those sharing events"
    )
    _add_tenant(p)
    p.set_defaults(handler=cmd_review_release)
    p = rev_sub.add_parser("reject", help="delete held material; a connection stays in review")
    p.add_argument("connection", nargs="?", metavar="CONNECTION", help="PROVIDER:IDENTIFIER")
    p.add_argument("--record", type=UUID, metavar="UUID", help="one record")
    _add_purge_flags(p)
    p.set_defaults(handler=cmd_review_reject)

    recs = sub.add_parser(
        "records", help="show, retire or restore a record, or set its severity or type"
    )
    recs_sub = recs.add_subparsers(dest="records_command", metavar="COMMAND", required=True)
    p = recs_sub.add_parser("show", help="print a record (redacted) with its scope and sources")
    p.add_argument("record_id", type=UUID, metavar="RECORD_ID")
    p.add_argument(
        "--raw",
        action="store_true",
        help="print the unredacted text, its sources and the messages they cite (audited)",
    )
    _add_tenant(p)
    p.set_defaults(handler=cmd_records_show)
    p = recs_sub.add_parser("severity", help="blocking fails the pull-request check")
    p.add_argument("record_id", type=UUID, metavar="RECORD_ID")
    p.add_argument("severity", choices=("blocking", "advisory"))
    _add_tenant(p)
    p.set_defaults(handler=cmd_records_severity)
    p = recs_sub.add_parser(
        "type", help="general is a reference record; the other types are decisions"
    )
    p.add_argument("record_id", type=UUID, metavar="RECORD_ID")
    p.add_argument("type", choices=("adr", "constraint", "business_requirement", "risk", "general"))
    _add_tenant(p)
    p.set_defaults(handler=cmd_records_type)
    p = recs_sub.add_parser("retire", help="stop serving a record; admins still see it")
    p.add_argument("record_id", type=UUID, metavar="RECORD_ID")
    p.add_argument(
        "--replaced-by", type=UUID, metavar="RECORD_ID", help="the live record that replaces it"
    )
    p.add_argument("--reason", help="kept with the record")
    _add_tenant(p)
    p.set_defaults(handler=cmd_records_retire)
    p = recs_sub.add_parser("restore", help="serve a retired record again")
    p.add_argument("record_id", type=UUID, metavar="RECORD_ID")
    _add_tenant(p)
    p.set_defaults(handler=cmd_records_restore)
    p = recs_sub.add_parser(
        "suggestions", help="list suggestions that a newer record replaces an older one"
    )
    _add_tenant(p)
    p.set_defaults(handler=cmd_records_suggestions)
    for name, text in (
        ("accept", "retire the older record, replaced by the newer"),
        ("dismiss", "keep both records"),
    ):
        p = recs_sub.add_parser(name, help=text)
        p.add_argument("suggestion_id", type=UUID, metavar="SUGGESTION_ID")
        _add_tenant(p)
        p.set_defaults(handler=cmd_records_decide)

    events = sub.add_parser("events", help="inspect and retry events the pipeline could not finish")
    events_sub = events.add_subparsers(dest="events_command", metavar="COMMAND", required=True)
    p = events_sub.add_parser(
        "failed", help="list events whose embedding step gave up and failed discussions"
    )
    p.add_argument("--limit", type=int, default=100, help="most rows to show (default: 100)")
    _add_tenant(p)
    p.set_defaults(handler=cmd_events_failed)
    p = events_sub.add_parser(
        "retry", help="put failed events and failed discussions back in the retry sweep"
    )
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument(
        "--id",
        dest="ids",
        type=UUID,
        action="append",
        metavar="UUID",
        help="event or discussion id (repeatable)",
    )
    which.add_argument("--all", action="store_true", help="every failed event and discussion")
    _add_tenant(p)
    p.set_defaults(handler=cmd_events_retry)

    dlq = sub.add_parser("dlq", help="inspect, replay or drop dead-letter stream entries")
    dlq_sub = dlq.add_subparsers(dest="dlq_command", metavar="COMMAND", required=True)
    p = dlq_sub.add_parser("list", help="list dead-letter streams with their entry counts")
    p.set_defaults(handler=cmd_dlq_list)
    p = dlq_sub.add_parser("show", help="list entries with their error and age (no content)")
    p.add_argument("stream", metavar="STREAM", help="a stream from `checkcord dlq list`")
    p.add_argument("--limit", type=int, default=50, help="most entries to show (default: 50)")
    p.set_defaults(handler=cmd_dlq_show)
    for name, handler, verb in (
        ("replay", cmd_dlq_replay, "re-add entries to their original stream"),
        ("drop", cmd_dlq_drop, "delete entries for good"),
    ):
        p = dlq_sub.add_parser(name, help=verb)
        p.add_argument("stream", metavar="STREAM", help="a stream from `checkcord dlq list`")
        which = p.add_mutually_exclusive_group(required=True)
        which.add_argument(
            "--id", dest="ids", action="append", metavar="ID", help="entry id (repeatable)"
        )
        which.add_argument("--all", action="store_true", help="every entry in the stream")
        _add_tenant(p)
        p.set_defaults(handler=handler)

    from checkcord.services.audiences import platforms

    aud = sub.add_parser("audiences", help="who can read each channel and repository")
    aud_sub = aud.add_subparsers(dest="audiences_command", metavar="COMMAND", required=True)
    p = aud_sub.add_parser("list", help="list stored audiences (counts, no members)")
    _add_tenant(p)
    p.set_defaults(handler=cmd_audiences_list)
    p = aud_sub.add_parser("refresh", help="read audiences from the provider now and apply them")
    p.add_argument("--platform", choices=platforms(), help="platform of --container")
    p.add_argument("--container", metavar="ID", help="channel id or OWNER/NAME repository")
    p.add_argument("--all", action="store_true", help="every stored audience")
    _add_tenant(p)
    p.set_defaults(handler=cmd_audiences_refresh)

    audit_p = sub.add_parser("audit", help="read the audit log")
    audit_sub = audit_p.add_subparsers(dest="audit_command", metavar="COMMAND", required=True)
    p = audit_sub.add_parser("list", help="list audit entries, newest first")
    _add_time_range(p)
    p.add_argument("--user", metavar="ID_OR_EMAIL", help="only this person")
    p.add_argument("--key", metavar="ID_OR_NAME", help="only this API key")
    p.add_argument("--action", help="only this action, e.g. mcp.search_conceptual_context")
    p.add_argument("--channel", choices=("mcp", "api", "cli", "scim"), help="only this channel")
    p.add_argument("--record", metavar="ID", help="only entries that returned this record")
    p.add_argument("--limit", type=int, default=50, help="most entries to show (default: 50)")
    p.add_argument("--cursor", help="continue from a previous page")
    _add_tenant(p)
    p.set_defaults(handler=cmd_audit_list)
    p = audit_sub.add_parser("export", help="write audit entries in a time range, oldest first")
    _add_time_range(p)
    p.add_argument("--format", choices=("jsonl", "csv"), default="jsonl", help="(default: jsonl)")
    p.add_argument("--output", metavar="FILE", help="write here instead of stdout")
    _add_tenant(p)
    p.set_defaults(handler=cmd_audit_export)
    p = audit_sub.add_parser("prune", help="delete old entries (uses MIGRATION_DATABASE_URL)")
    p.add_argument("--days", type=int, help="older than this (default: AUDIT_RETENTION_DAYS)")
    p.add_argument("--dry-run", action="store_true", help="print the count only")
    _add_yes(p)
    p.set_defaults(handler=cmd_audit_prune)

    red = sub.add_parser("redaction", help="stored redacted text of records")
    red_sub = red.add_subparsers(dest="redaction_command", metavar="COMMAND", required=True)
    p = red_sub.add_parser(
        "recompute", help="redact again records stored under older redaction rules or model"
    )
    p.add_argument(
        "--batch-size", type=_positive, default=100, help="records per batch (default: 100)"
    )
    p.add_argument(
        "--tenant", type=UUID, metavar="UUID", help="one tenant (default: every known tenant)"
    )
    p.set_defaults(handler=cmd_redaction_recompute)

    p = sub.add_parser("usage", help="model calls, tokens and estimated cost")
    p.add_argument("--days", type=_positive, default=30, help="this many days back (default: 30)")
    _add_tenant(p)
    p.set_defaults(handler=cmd_usage)

    p = sub.add_parser("stats", help="events, records, agent reads and checks")
    p.add_argument("--days", type=_positive, default=7, help="this many days back (default: 7)")
    _add_tenant(p)
    p.set_defaults(handler=cmd_stats)

    demo_p = sub.add_parser("demo", help="load or remove sample data for an invented company")
    demo_sub = demo_p.add_subparsers(dest="demo_command", metavar="COMMAND", required=True)
    p = demo_sub.add_parser("load", help="send the sample story through the pipeline")
    p.add_argument(
        "--wait",
        type=float,
        default=300,
        metavar="SECONDS",
        help="wait this long for the worker to finish (default: 300; 0 to not wait)",
    )
    p.add_argument("--public-url", help="API address for the printed examples")
    _add_tenant(p)
    p.set_defaults(handler=cmd_demo_load)
    p = demo_sub.add_parser("remove", help="delete the sample data")
    _add_tenant(p)
    p.set_defaults(handler=cmd_demo_remove)

    p = sub.add_parser("doctor", help="check configuration and dependencies")
    p.set_defaults(handler=cmd_doctor)

    proxy.add_parser(sub)
    return parser


class _Stderr:
    def write(self, text: str) -> int:
        return sys.stderr.write(text)

    def flush(self) -> None:
        sys.stderr.flush()


SERVERS = frozenset({"api", "worker"})
LOUD_LEVELS = {"debug": 10, "info": 20}


def _add_verbose(parser: argparse.ArgumentParser, default: Any) -> None:
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=default,
        help="also print info log lines (audit entries are written either way)",
    )
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                _add_verbose(child, argparse.SUPPRESS)


def log_level(args: argparse.Namespace) -> int:
    """WARNING for admin commands unless asked for more; servers keep LOG_LEVEL."""
    import logging
    import os

    if args.command in SERVERS:
        return logging.NOTSET
    if getattr(args, "verbose", False):
        return logging.INFO
    return LOUD_LEVELS.get(os.environ.get("LOG_LEVEL", "").strip().lower(), logging.WARNING)


def _quiet_logging(level: int) -> None:
    import logging

    import structlog

    # Log lines, audit entries included, go to stderr so stdout carries only the output.
    logging.basicConfig(stream=_Stderr(), format="%(message)s", level=level or logging.WARNING)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(_Stderr()),
        cache_logger_on_first_use=False,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    _add_verbose(parser, False)
    args = parser.parse_args(argv)
    if args.command not in SERVERS:
        _quiet_logging(log_level(args))
    try:
        args.handler(args)
    except CLIError as e:
        print(f"checkcord: error: {e}", file=sys.stderr)
        sys.exit(1)

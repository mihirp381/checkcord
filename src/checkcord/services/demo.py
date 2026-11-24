"""Sample data for trying Checkcord: a small invented company, sent through the real pipeline.

Every sample record's text starts with ``[Sample]``. Sample events come through the
``custom:sample`` and ``custom:sample-vendor-wiki`` connections; the second is in review.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.ast.parser import TreeSitterParser
from checkcord.bus.interface import EventBus
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.config import get_settings
from checkcord.models.common import AccessControl, AuthorInfo, CodeMetadata
from checkcord.models.enums import AccessVisibility, AclType, SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.services import purge, review
from checkcord.services.ast_writer import write_ast_nodes_for_file
from checkcord.services.connections import connection_key
from checkcord.services.keys import create_key
from checkcord.storage import audiences, audit, event_pipeline
from checkcord.storage.models import (
    ASTNode,
    AudienceContainer,
    ConnectionReview,
    ContextEventLink,
    EnterpriseUser,
)
from checkcord.storage.postgres import PostgresEventStore, events_table, tenant_context

MARK = "[Sample]"
COMPANY = "Brambleway Freight"
DOMAIN = "brambleway.example"
REPO = "brambleway/dispatch-api"
LIVE = ("custom", "sample")
IN_REVIEW = ("custom", "sample-vendor-wiki")
SEARCH = {"query": "carrier webhook secret", "repo_identifier": REPO}
PUBLIC_CHANNEL = "#dispatch-eng"
PRIVATE_CHANNEL = "#security-review"

# key -> (display name, role)
PEOPLE = {
    "mara": ("Mara Quist", "platform lead"),
    "tobin": ("Tobin Reyes-Hart", "backend engineer"),
    "juno": ("Juno Aldana", "security engineer"),
    "pell": ("Pell Okonjo", "contractor"),
}
PRIVATE_MEMBERS = ("mara", "juno")

FILES = {
    "dispatch/quotes.py": """class CarrierClient:
    def get(self, path):
        return {}


def rebuild_rate_table():
    pass


def fetch_live_quote(carrier, parcel):
    return CarrierClient().get(f"/quotes/{carrier}")


def quote_total(quote):
    return quote["cents"]
""",
    "dispatch/drivers.py": """def assign_driver(shipment):
    return None
""",
    "dispatch/webhooks.py": """def verify_webhook_signature(body, signature, secret):
    return False


def rotate_webhook_signer(carrier):
    pass
""",
    "dispatch/log_filters.py": """def mask_phone(value):
    return "***"
""",
}


@dataclass(frozen=True)
class Item:
    days_ago: int
    author: str
    text: str
    kind: str = "general"
    channel: str | None = PUBLIC_CHANNEL
    connection: tuple[str, str] = LIVE


STORY = [
    Item(
        40,
        "mara",
        "Decision: dispatch-api builds shipping quotes in a nightly batch job, "
        "rebuild_rate_table, and serves them from the rate cache. Checkout never calls a "
        "carrier live.",
        "adr",
    ),
    Item(
        38,
        "tobin",
        "Decision: dispatch-api stores money as integer cents. quote_total returns cents; "
        "never use floating point for prices.",
        "adr",
    ),
    Item(
        35,
        "mara",
        "Constraint: every carrier API call goes through CarrierClient, so retries and the "
        "3-second timeout live in one place.",
        "constraint",
    ),
    Item(
        30,
        "tobin",
        "Risk: assign_driver still reads the legacy drivers table. The nightly sync can lag "
        "by an hour, so one driver can be booked twice.",
        "risk",
    ),
    Item(
        26,
        "mara",
        "Decision: the public API is versioned in the path (/v2/...). v1 is frozen: bug "
        "fixes only, no new fields.",
        "adr",
    ),
    Item(
        21,
        "tobin",
        "Decision: dispatch-api logs never contain customer phone numbers; mask_phone runs "
        "in the log filter. Questions go to rates-team@brambleway.example or the on-call "
        "line +1-555-0142.",
        "constraint",
    ),
    Item(
        14,
        "juno",
        "Restricted: a carrier webhook secret was pasted into a support ticket. It was "
        "rotated with rotate_webhook_signer; incident notes stay in #security-review.",
        "risk",
        PRIVATE_CHANNEL,
    ),
    Item(
        13,
        "juno",
        "Decision (security review only): carrier webhooks are rejected unless "
        "verify_webhook_signature passes an HMAC-SHA256 check, before any parsing.",
        "adr",
        PRIVATE_CHANNEL,
    ),
    Item(
        7,
        "mara",
        "Decision update: quotes are now fetched live from carriers with fetch_live_quote "
        "and cached for 10 minutes. This supersedes the nightly batch decision; "
        "rebuild_rate_table is being removed.",
        "adr",
    ),
    Item(
        5,
        "tobin",
        "Risk: fetch_live_quote has no circuit breaker yet; if a carrier is slow, checkout "
        "waits the full CarrierClient timeout.",
        "risk",
    ),
    Item(
        3,
        "pell",
        "Vendor wiki: the route optimizer may move to a hosted service next quarter; "
        "nothing is decided.",
        channel=None,
        connection=IN_REVIEW,
    ),
    Item(
        2,
        "pell",
        "Vendor wiki: sandbox carriers accept any API version header, so tests against "
        "them can pass while production rejects the same call.",
        "risk",
        channel=None,
        connection=IN_REVIEW,
    ),
]


class SampleDataExistsError(Exception):
    pass


@dataclass
class Loaded:
    people: dict[str, UUID]
    keys: dict[str, str]
    event_ids: list[UUID]
    code_symbols: int


@dataclass
class Progress:
    linked: int
    failed: int
    total: int

    @property
    def finished(self) -> bool:
        return self.linked + self.failed >= self.total


@dataclass
class Removed:
    events: int = 0
    records: int = 0
    people: int = 0
    keys: int = 0
    code_symbols: int = 0
    audiences: int = 0


def email(person: str) -> str:
    return f"{person}@{DOMAIN}"


def _access(is_public: bool) -> AccessControl:
    if is_public:
        return AccessControl(
            is_public=True, visibility=AccessVisibility.PUBLIC, acl_type=AclType.OPEN
        )
    return AccessControl(
        is_public=False, visibility=AccessVisibility.PRIVATE, acl_type=AclType.CHANNEL_MEMBERS
    )


def build_events(tenant_id: UUID, people: dict[str, UUID], now: datetime) -> list[CanonicalEvent]:
    events = []
    for item in STORY:
        event_id = uuid4()
        text = f"{MARK} {item.text}"
        name = PEOPLE[item.author][0]
        events.append(
            CanonicalEvent(
                event_id=event_id,
                tenant_id=tenant_id,
                source_platform=SourcePlatform.CUSTOM,
                source_type=SourceType.CUSTOM_PAYLOAD,
                source_id=f"sample-{event_id}",
                happened_at=now - timedelta(days=item.days_ago),
                author=AuthorInfo(
                    source_id=email(item.author),
                    internal_id=people[item.author],
                    email=email(item.author),
                    display_name=name,
                ),
                content_raw=text,
                content_clean=text,
                access_control=_access(item.channel != PRIVATE_CHANNEL),
                audience_container=item.channel,
                connection=connection_key(*item.connection),
                code_metadata=CodeMetadata(repository=REPO, branch="main"),
                metadata={"context_type": item.kind, "sample": True},
            )
        )
    return events


async def is_loaded(engine: AsyncEngine, tenant_id: UUID) -> bool:
    async with tenant_context(engine, tenant_id) as conn:
        found = await conn.scalar(
            select(func.count())
            .select_from(EnterpriseUser)
            .where(
                (EnterpriseUser.tenant_id == tenant_id)
                & func.lower(EnterpriseUser.primary_email).in_([email(p) for p in PEOPLE])
            )
        )
    return bool(found)


async def _people(engine: AsyncEngine, tenant_id: UUID) -> dict[str, UUID]:
    ids = {person: uuid4() for person in PEOPLE}
    async with tenant_context(engine, tenant_id) as conn:
        for person, (name, _) in PEOPLE.items():
            await conn.execute(
                EnterpriseUser.__table__.insert().values(
                    id=ids[person],
                    tenant_id=tenant_id,
                    primary_email=email(person),
                    display_name=f"{name} (sample)",
                    active=True,
                )
            )
    return ids


async def _code(engine: AsyncEngine, tenant_id: UUID) -> int:
    parser = TreeSitterParser()
    written = 0
    async with tenant_context(engine, tenant_id) as conn:
        store = PostgresEventStore(conn)
        for path, source in FILES.items():
            result = await write_ast_nodes_for_file(
                tenant_id, REPO, path, source.encode(), store, parser
            )
            written += result.nodes_written
    return written


async def _audiences(engine: AsyncEngine, tenant_id: UUID, people: dict[str, UUID]) -> None:
    settings = get_settings()
    members = [str(people[p]) for p in PRIVATE_MEMBERS]
    for container, target in (
        (PUBLIC_CHANNEL, audiences.Audience(True)),
        (PRIVATE_CHANNEL, audiences.Audience.of(False, members)),
    ):
        await audiences.apply(
            engine,
            tenant_id,
            "custom",
            container,
            target,
            now=datetime.now(UTC),
            grace=timedelta(hours=settings.audience_refresh_grace_hours),
            batch_size=settings.audience_refresh_batch_size,
            details={"sample": True},
        )


async def load(
    engine: AsyncEngine, redis: Redis, tenant_id: UUID, bus: EventBus | None = None
) -> Loaded:
    """Create the sample people, code, audiences and review state, then publish the story."""
    if await is_loaded(engine, tenant_id):
        raise SampleDataExistsError(tenant_id)
    people = await _people(engine, tenant_id)
    keys = {}
    for person in ("mara", "pell"):
        _, token = await create_key(
            engine, tenant_id, f"sample: {PEOPLE[person][0]}", False, user_email=email(person)
        )
        keys[person] = token
    symbols = await _code(engine, tenant_id)
    await _audiences(engine, tenant_id, people)
    async with tenant_context(engine, tenant_id) as conn:
        await review.mark_new(conn, tenant_id, connection_key(*LIVE), review=False)
        await review.mark_new(conn, tenant_id, connection_key(*IN_REVIEW), review=True)
        events = build_events(tenant_id, people, datetime.now(UTC))
        await audit.record(
            conn,
            tenant_id,
            audit.CLI_ACTOR,
            "sample.loaded",
            params={"events": len(events), "people": len(people)},
            result_count=len(events),
        )
    bus = bus or RedisStreamsEventBus(redis)
    for event in events:
        await bus.publish("events.raw.custom", event)
    return Loaded(people, keys, [e.event_id for e in events], symbols)


async def progress(engine: AsyncEngine, tenant_id: UUID, event_ids: list[UUID]) -> Progress:
    async with tenant_context(engine, tenant_id, include_held=True) as conn:
        linked = await conn.scalar(
            select(func.count(func.distinct(ContextEventLink.event_id))).where(
                (ContextEventLink.tenant_id == tenant_id) & ContextEventLink.event_id.in_(event_ids)
            )
        )
        failed = await conn.scalar(
            select(func.count())
            .select_from(events_table)
            .where(
                (events_table.c.tenant_id == tenant_id)
                & events_table.c.event_id.in_(event_ids)
                & (events_table.c.pipeline_state == event_pipeline.FAILED)
            )
        )
    return Progress(int(linked or 0), int(failed or 0), len(event_ids))


async def wait(
    engine: AsyncEngine,
    tenant_id: UUID,
    event_ids: list[UUID],
    seconds: float,
    interval: float = 2.0,
) -> Progress:
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        state = await progress(engine, tenant_id, event_ids)
        if state.finished or asyncio.get_running_loop().time() >= deadline:
            return state
        await asyncio.sleep(interval)


async def remove(engine: AsyncEngine, redis: Redis | None, tenant_id: UUID) -> Removed:
    """Delete everything `load` created except audit entries, which cannot be changed."""
    removed = Removed()
    for provider, identifier in (LIVE, IN_REVIEW):
        purged = await purge.purge(
            engine,
            tenant_id,
            purge.connection(provider, identifier),
            actor=audit.CLI_ACTOR,
            redis=redis,
            audit_empty=False,
        )
        removed.events += purged.events
        removed.records += purged.records_deleted
        removed.audiences += purged.containers
    for person in PEOPLE:
        deleted = await purge.delete_person(
            engine,
            tenant_id,
            actor=audit.CLI_ACTOR,
            email=email(person),
            redis=redis,
            remember=False,
        )
        if deleted is not None:
            removed.people += 1
            removed.keys += deleted.keys
            removed.events += deleted.purged.events
            removed.records += deleted.purged.records_deleted
    async with tenant_context(engine, tenant_id, include_held=True) as conn:
        gone = await conn.execute(
            delete(ASTNode).where(
                (ASTNode.tenant_id == tenant_id) & (ASTNode.repo_identifier == REPO)
            )
        )
        removed.code_symbols = gone.rowcount
        await conn.execute(
            delete(ConnectionReview).where(
                (ConnectionReview.tenant_id == tenant_id)
                & ConnectionReview.connection.in_(
                    [connection_key(*LIVE), connection_key(*IN_REVIEW)]
                )
            )
        )
        containers = AudienceContainer.__table__
        left = await conn.execute(
            delete(containers).where(
                (containers.c.tenant_id == tenant_id)
                & (containers.c.platform == "custom")
                & containers.c.container.in_([PUBLIC_CHANNEL, PRIVATE_CHANNEL])
            )
        )
        removed.audiences += left.rowcount
        await audit.record(
            conn,
            tenant_id,
            audit.CLI_ACTOR,
            "sample.removed",
            params={"events": removed.events, "records": removed.records},
            result_count=removed.records,
        )
    return removed


def next_steps(loaded: Loaded, api_url: str, web_url: str) -> str:
    mcp = f"{api_url.rstrip('/')}/mcp"
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "search_conceptual_context",
            "arguments": SEARCH,
        },
    }
    mara = f"{PEOPLE['mara'][0]}, in {PRIVATE_CHANNEL}:"
    pell = f"{PEOPLE['pell'][0]}, outside it:"
    width = max(len(mara), len(pell)) + 1
    return f"""Sample data for {COMPANY}, an invented company, is on its way through the pipeline.

Console:  {web_url}  (sign in with your admin key)
  Records   twelve events become about ten records, all starting with {MARK}
  Review    two vendor-wiki records wait for release or rejection
  Suggested the live-quote decision says it replaces the nightly batch one; accept to retire it
  Audiences {PRIVATE_CHANNEL} is readable by {PEOPLE["mara"][0]} and {PEOPLE["juno"][0]} only
  Graph     repository {REPO}

Sample keys (shown once):
  {mara:<{width}}{loaded.keys["mara"]}
  {pell:<{width}}{loaded.keys["pell"]}

Ask the MCP server as {PEOPLE["mara"][0]}:
  curl -s {mcp} \\
    -H "Authorization: Bearer {loaded.keys["mara"]}" \\
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \\
    -d '{json.dumps(call)}'

Run it again with {PEOPLE["pell"][0]}'s key: the {PRIVATE_CHANNEL} records are left out.

Agent config (Claude Code, Cursor and others that speak streamable HTTP):
  {{"mcpServers": {{"checkcord": {{"type": "http", "url": "{mcp}",
    "headers": {{"Authorization": "Bearer <a person's key>"}}}}}}}}

Remove it all with `checkcord demo remove`."""

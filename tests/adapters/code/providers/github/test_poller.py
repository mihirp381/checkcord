"""
Unit tests for the GitHub polling fallback (``poller.py``).

Strategy
--------
* We do NOT spin up a real database.  Instead we mock the SQLAlchemy engine
  to return a pre-configured list of ``GithubInstallation`` rows.
* The ``httpx.AsyncClient`` is mocked via ``respx`` or ``unittest.mock`` so
  that no real network calls are made.
* We use ``fakeredis.aioredis.FakeRedis`` for an in-process Redis that is
  fully restarted between tests.
* The parser functions (``parse_pull_request_event``, etc.) are mocked to
  return dummy ``CanonicalEvent`` objects, keeping the test focused on the
  poller's orchestration logic.

What we verify
--------------
1. The poller reads all installations from the DB (bypassing RLS).
2. For each installation it GETs the GitHub ``/events`` endpoint.
3. Events newer than the last checkpoint are processed; duplicates are skipped.
4. After processing, the Redis checkpoint keys are updated.
5. Non-200 HTTP responses are handled gracefully (no crash, continue to next
   installation).
6. Parse errors are tolerated (checkpoint still advances past the bad event).
"""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import fakeredis.aioredis
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.code.providers.github.poller import poll_github_history
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.models.enums import (
    AccessVisibility,
    AclType,
    EventAction,
    SourcePlatform,
    SourceType,
)
from checkcord.models.event import CanonicalEvent

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

REPO_IDENTIFIER = "test_org/poll_repo"
TENANT_ID = uuid4()
PAT = "test_poll_pat"


@pytest_asyncio.fixture
async def fake_redis() -> Any:
    """In-process Redis backed by fakeredis."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    yield client
    await client.flushall()
    await client.aclose()


@pytest.fixture(autouse=True)
def everything_in_scope() -> Any:
    with patch("checkcord.services.scope.allows", AsyncMock(return_value=True)):
        yield


@pytest.fixture
def mock_db_engine() -> MagicMock:
    """Mock AsyncEngine that yields one GithubInstallation row."""
    engine = MagicMock(spec=AsyncEngine)
    conn = AsyncMock()
    result = MagicMock()
    result.mappings.return_value.fetchall.return_value = [
        {
            "repo_identifier": REPO_IDENTIFIER,
            "tenant_id": TENANT_ID,
            "personal_access_token": PAT,
        }
    ]
    conn.execute = AsyncMock(return_value=result)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    engine.connect.return_value = conn
    return engine


def _make_github_event(
    event_id: str,
    event_type: str,
    created_at: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Helper to build a minimal GitHub API /events response item.

    The ``payload`` field must include enough data to pass schema validation in
    the poller (which calls ``GitHubPRPayload.model_validate(payload)`` etc.
    before calling the parser function).
    """
    if payload is None and event_type == "PullRequestEvent":
        payload = {
            "action": "opened",
            "number": 1,
            "pull_request": {
                "number": 1,
                "title": "Test PR",
                "body": None,
                "created_at": created_at,
                "html_url": "https://github.com/test/repo/pull/1",
                "user": {"login": "coder", "email": None},
                "head": {"ref": "feature", "sha": "abc123"},
                "base": {"ref": "main"},
            },
        }
    return {
        "id": event_id,
        "type": event_type,
        "created_at": created_at,
        "payload": payload or {},
    }


def _make_canonical_event() -> CanonicalEvent:
    """Create a minimal CanonicalEvent for use as a mock parser return value."""
    return CanonicalEvent(
        event_id=uuid4(),
        tenant_id=TENANT_ID,
        source_platform=SourcePlatform.GITHUB,
        source_type=SourceType.PR_OPENED,
        event_action=EventAction.CREATED,
        source_id=f"pr:{REPO_IDENTIFIER}:1",
        version=1,
        happened_at=datetime.now(UTC),
        author=AuthorInfo(source_id="coder"),
        content_raw="PR opened",
        content_clean="PR opened",
        access_control=AccessControl(
            is_public=True,
            visibility=AccessVisibility.PUBLIC,
            acl_type=AclType.OPEN,
            allowed_identity_ids=[],
        ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poller_processes_new_events(
    mock_db_engine: MagicMock,
    fake_redis: Any,
) -> None:
    """
    The poller must process events that are newer than the last checkpoint
    and publish them to the Redis stream.

    *Why:* This verifies the core happy path — that the poller finds missed
    events and forwards them to the same processing pipeline as webhooks.
    """
    bus = RedisStreamsEventBus(fake_redis)

    # Use a future timestamp so the event is newer than the poller's default
    # "now" checkpoint (which defaults to the current time when no prior
    # checkpoint exists).
    github_events = [
        _make_github_event("evt_002", "PullRequestEvent", "2099-01-01T00:00:00Z"),
    ]

    dummy_event = _make_canonical_event()

    with (
        patch("httpx.AsyncClient") as MockClient,
        patch(
            "checkcord.adapters.code.providers.github.poller.parse_pull_request_event",
            new_callable=AsyncMock,
            return_value=dummy_event,
        ),
    ):
        # Mock the httpx response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = github_events
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        await poll_github_history(mock_db_engine, bus)

    # Should have published one event to the stream
    stream_messages = await fake_redis.xread(streams={"events.raw.code": "0-0"}, count=10)
    assert stream_messages, "Expected at least one event published to Redis stream"
    assert len(stream_messages[0][1]) == 1


@pytest.mark.asyncio
async def test_poller_checkpoint_updated(
    mock_db_engine: MagicMock,
    fake_redis: Any,
) -> None:
    """
    After successfully processing events, the poller must update the Redis
    checkpoint keys (``github:last_event_id:<repo>`` and
    ``github:last_event_time:<repo>``).

    *Why:* The checkpoint is the deduplication mechanism — without it, the
    poller would reprocess every event on the next run.
    """
    bus = RedisStreamsEventBus(fake_redis)
    repo_key = REPO_IDENTIFIER

    github_events = [
        _make_github_event("evt_latest", "PullRequestEvent", "2099-01-01T01:00:00Z"),
    ]

    dummy_event = _make_canonical_event()

    with (
        patch("httpx.AsyncClient") as MockClient,
        patch(
            "checkcord.adapters.code.providers.github.poller.parse_pull_request_event",
            new_callable=AsyncMock,
            return_value=dummy_event,
        ),
    ):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = github_events
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        await poll_github_history(mock_db_engine, bus)

    # Check checkpoint was saved
    last_id = await fake_redis.get(f"github:last_event_id:{repo_key}")
    last_time = await fake_redis.get(f"github:last_event_time:{repo_key}")
    assert last_id is not None, "last_event_id checkpoint not set"
    assert last_id.decode("utf-8") == "evt_latest"
    assert last_time is not None, "last_event_time checkpoint not set"


@pytest.mark.asyncio
async def test_poller_skips_already_seen_events(
    mock_db_engine: MagicMock,
    fake_redis: Any,
) -> None:
    """
    The poller must skip events that are at or before the last checkpoint
    time to avoid publishing duplicates.

    *Why:* Webhooks and polling can overlap.  Without deduplication we would
    publish the same event twice, corrupting the downstream expertise graph.
    """
    bus = RedisStreamsEventBus(fake_redis)
    repo_key = REPO_IDENTIFIER

    # Set checkpoint to "already seen" time
    checkpoint_time = "2026-06-24T08:00:00"
    await fake_redis.set(f"github:last_event_id:{repo_key}", "evt_old")
    await fake_redis.set(f"github:last_event_time:{repo_key}", checkpoint_time)

    # Return only old events (at or before checkpoint)
    github_events = [
        _make_github_event("evt_old", "PullRequestEvent", "2026-06-24T07:58:00Z"),
    ]

    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = github_events
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        with patch(
            "checkcord.adapters.code.providers.github.poller.parse_pull_request_event",
            new_callable=AsyncMock,
        ) as mock_parser:
            await poll_github_history(mock_db_engine, bus)
            # Parser must NOT have been called because the event is in the past
            mock_parser.assert_not_called()

    # No new events should have been published
    stream_messages = await fake_redis.xread(streams={"events.raw.code": "0-0"}, count=10)
    assert not stream_messages, "Expected no events published for already-seen events"


@pytest.mark.asyncio
async def test_poller_non_200_response_is_handled_gracefully(
    mock_db_engine: MagicMock,
    fake_redis: Any,
) -> None:
    """
    If the GitHub API returns a non-200 status, the poller must not crash.
    It should log the error and move on to the next installation.

    *Why:* Rate-limiting (429) or temporary outages (503) should never bring
    down the whole polling loop.
    """
    bus = RedisStreamsEventBus(fake_redis)

    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.text = "rate limit exceeded"
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        # Should not raise
        await poll_github_history(mock_db_engine, bus)

    # Nothing published
    stream_messages = await fake_redis.xread(streams={"events.raw.code": "0-0"}, count=10)
    assert not stream_messages


@pytest.mark.asyncio
async def test_poller_parse_error_advances_checkpoint(
    mock_db_engine: MagicMock,
    fake_redis: Any,
) -> None:
    """
    When a parse error occurs for one event, the poller must still advance
    the checkpoint past that event so future runs don't get stuck in a loop.

    *Why:* A malformed payload from GitHub (e.g. schema change) should not
    permanently block the polling pipeline.
    """
    bus = RedisStreamsEventBus(fake_redis)
    repo_key = REPO_IDENTIFIER

    github_events = [
        _make_github_event("evt_bad", "PullRequestEvent", "2099-01-01T09:00:00Z"),
    ]

    with (
        patch("httpx.AsyncClient") as MockClient,
        patch(
            "checkcord.adapters.code.providers.github.poller.parse_pull_request_event",
            new_callable=AsyncMock,
            side_effect=ValueError("Unexpected payload structure"),
        ),
    ):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = github_events
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        # Should not raise
        await poll_github_history(mock_db_engine, bus)

    # Checkpoint must advance to the bad event's ID so we don't retry it
    last_id = await fake_redis.get(f"github:last_event_id:{repo_key}")
    assert last_id is not None, "Checkpoint was not updated after parse error"
    assert last_id.decode("utf-8") == "evt_bad"


@pytest.mark.asyncio
async def test_poller_multiple_installations(
    fake_redis: Any,
) -> None:
    """
    When multiple GithubInstallations exist, the poller must process each one
    independently and update each installation's checkpoint separately.

    *Why:* Multiple customers may have different repos; their polling state must
    not bleed into each other.
    """
    tenant_a = uuid4()
    tenant_b = uuid4()
    repo_a = "org_a/repo_a"
    repo_b = "org_b/repo_b"

    engine = MagicMock(spec=AsyncEngine)
    conn = AsyncMock()
    result = MagicMock()
    result.mappings.return_value.fetchall.return_value = [
        {"repo_identifier": repo_a, "tenant_id": tenant_a, "personal_access_token": "pat_a"},
        {"repo_identifier": repo_b, "tenant_id": tenant_b, "personal_access_token": "pat_b"},
    ]
    conn.execute = AsyncMock(return_value=result)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    engine.connect.return_value = conn

    bus = RedisStreamsEventBus(fake_redis)

    # Use far-future timestamps so the events are newer than the default "now" checkpoint
    events_a = [_make_github_event("evt_a1", "PullRequestEvent", "2099-01-01T10:00:00Z")]
    events_b = [_make_github_event("evt_b1", "PullRequestEvent", "2099-01-01T10:00:00Z")]

    call_count = 0

    async def _side_effect_get(url: str, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        mock_response = MagicMock()
        mock_response.status_code = 200
        if "org_a" in url:
            mock_response.json.return_value = events_a
        else:
            mock_response.json.return_value = events_b
        return mock_response

    dummy_event_a = _make_canonical_event()

    with (
        patch("httpx.AsyncClient") as MockClient,
        patch(
            "checkcord.adapters.code.providers.github.poller.parse_pull_request_event",
            new_callable=AsyncMock,
            return_value=dummy_event_a,
        ),
    ):
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(side_effect=_side_effect_get)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = mock_http_client

        await poll_github_history(engine, bus)

    # Both installations polled
    assert call_count == 2

    # Each checkpoint saved independently
    last_id_a = await fake_redis.get(f"github:last_event_id:{repo_a}")
    last_id_b = await fake_redis.get(f"github:last_event_id:{repo_b}")
    assert last_id_a is not None
    assert last_id_b is not None
    assert last_id_a.decode("utf-8") == "evt_a1"
    assert last_id_b.decode("utf-8") == "evt_b1"


def _numbered(n: int) -> dict[str, Any]:
    created = f"2099-01-01T00:{n // 60:02d}:{n % 60:02d}Z"
    event = _make_github_event(str(n), "PullRequestEvent", created)
    event["payload"]["number"] = n
    return event


class FakeEvents:
    """The repository events API: newest first, numbered pages, at most `limit` served."""

    def __init__(self, count: int, limit: int = 300) -> None:
        self.events = [_numbered(n) for n in range(1, count + 1)]
        self.limit = limit
        self.fail_page: int | None = None

    async def send(self, _p: str, _c: Any, _m: str, _url: str, **kwargs: Any) -> Any:
        page, size = kwargs["params"]["page"], kwargs["params"]["per_page"]
        response = MagicMock()
        if page == self.fail_page:
            self.fail_page = None
            response.status_code = 502
            return response
        served = list(reversed(self.events))[: self.limit]
        response.status_code = 200
        response.json.return_value = served[(page - 1) * size : page * size]
        return response


class Recorder:
    def __init__(self, fail_on: int | None = None) -> None:
        self.fail_on = fail_on
        self.published: list[int] = []

    async def publish(self, topic: str, event: CanonicalEvent) -> str:
        number = int(event.source_id)
        if number == self.fail_on:
            self.fail_on = None
            raise ConnectionError("stream unavailable")
        self.published.append(number)
        return "0-1"


async def _parse(payload: Any, *_args: Any) -> CanonicalEvent:
    return _make_canonical_event().model_copy(update={"source_id": str(payload.number)})


async def _poll(provider: FakeEvents, recorder: Recorder, engine: MagicMock, redis: Any) -> None:
    from checkcord.adapters.code.providers.github import poller

    with (
        patch.object(poller.http_retry, "send", provider.send),
        patch.object(poller, "parse_pull_request_event", _parse),
        patch.object(RedisStreamsEventBus, "publish", lambda _s, **kw: recorder.publish(**kw)),
    ):
        await poll_github_history(engine, RedisStreamsEventBus(redis))


@pytest.fixture
def small_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    from checkcord.adapters.code.providers.github import poller

    monkeypatch.setattr(poller, "PAGE_SIZE", 50)


async def _seed(redis: Any) -> None:
    await redis.set(f"github:last_event_id:{REPO_IDENTIFIER}", "0")
    await redis.set(f"github:last_event_time:{REPO_IDENTIFIER}", "2098-12-31T00:00:00")


@pytest.mark.usefixtures("small_pages")
async def test_poller_pages_back_and_publishes_oldest_first(
    mock_db_engine: MagicMock, fake_redis: Any
) -> None:
    await _seed(fake_redis)
    provider, recorder = FakeEvents(120), Recorder()
    await _poll(provider, recorder, mock_db_engine, fake_redis)
    assert recorder.published == list(range(1, 121))
    await _poll(provider, recorder, mock_db_engine, fake_redis)
    assert recorder.published == list(range(1, 121))


@pytest.mark.usefixtures("small_pages")
async def test_poller_keeps_its_place_when_a_page_fails(
    mock_db_engine: MagicMock, fake_redis: Any
) -> None:
    await _seed(fake_redis)
    provider, recorder = FakeEvents(120), Recorder()
    provider.fail_page = 2
    await _poll(provider, recorder, mock_db_engine, fake_redis)
    assert recorder.published == []
    await _poll(provider, recorder, mock_db_engine, fake_redis)
    assert recorder.published == list(range(1, 121))


@pytest.mark.usefixtures("small_pages")
async def test_poller_keeps_its_place_when_publishing_fails(
    mock_db_engine: MagicMock, fake_redis: Any
) -> None:
    await _seed(fake_redis)
    provider = FakeEvents(120)
    recorder = Recorder(fail_on=30)
    await _poll(provider, recorder, mock_db_engine, fake_redis)
    assert recorder.published == list(range(1, 30))
    await _poll(provider, recorder, mock_db_engine, fake_redis)
    assert recorder.published == list(range(1, 121))


@pytest.mark.usefixtures("small_pages")
async def test_poller_reads_the_whole_api_window_and_no_further(
    mock_db_engine: MagicMock, fake_redis: Any
) -> None:
    await _seed(fake_redis)
    provider = FakeEvents(200, limit=150)
    recorder = Recorder()
    await _poll(provider, recorder, mock_db_engine, fake_redis)
    assert recorder.published == list(range(51, 201))

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
import structlog

from checkcord.adapters.ingestion import poll_cursor
from checkcord.adapters.tasks.providers.jira import document_poller as jira
from checkcord.adapters.tasks.providers.linear import router as linear
from checkcord.adapters.tasks.providers.notion import router as notion

ITEMS = 120
NOW = datetime.now(UTC)
LOG = structlog.get_logger(__name__)


class Bus:
    def __init__(self) -> None:
        self.published: list[str] = []

    async def publish(self, topic: str, event: Any) -> None:
        self.published.append(event)


class _Event(str):
    def model_copy(self, **_kwargs: Any) -> str:
        return str(self)


async def _issue_events(_engine: Any, _tenant: Any, issue: dict[str, Any]) -> list[_Event]:
    return [_Event(issue["key"])]


class Store:
    """Keeps what the poller saves, as the installation row would."""

    def __init__(self, **row: Any) -> None:
        self.row = SimpleNamespace(
            id=uuid4(), tenant_id=uuid4(), poll_cursor=None, poll_resume=None, **row
        )

    async def save(self, _engine: Any, _model: Any, _row_id: Any, **values: Any) -> None:
        for name, value in values.items():
            setattr(self.row, name, value)


def _response(body: Any) -> httpx.Response:
    return httpx.Response(200, json=body, request=httpx.Request("POST", "https://provider.test"))


@pytest.fixture(autouse=True)
def shared(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with (
        patch("checkcord.services.scope.allows", AsyncMock(return_value=True)),
        patch.object(linear, "decrypt_secret", lambda v: v),
        patch.object(jira, "decrypt_secret", lambda v: v),
        patch.object(notion, "decrypt_secret", lambda v: v),
        patch.object(linear, "issue_events", lambda _a, _t, issue, *_r: [issue["identifier"]]),
        patch.object(jira, "issue_events", _issue_events),
        patch.object(notion, "document_to_canonical_event", lambda doc, *_a, **_k: doc),
    ):
        yield


class FakeLinear:
    """Issues ordered newest first, as Linear returns them, with Relay first/last paging."""

    def __init__(self) -> None:
        self.issues = [
            {
                "id": f"id-{n}",
                "identifier": f"ENG-{n}",
                "updatedAt": (NOW - timedelta(hours=1) + timedelta(seconds=n)).isoformat(),
                "comments": {
                    "nodes": [{"id": f"c{n}-0"}],
                    "pageInfo": {"hasNextPage": n == 7, "endCursor": "c1"},
                },
            }
            for n in range(ITEMS)
        ]
        self.fail_on: int | None = None
        self.issue_pages = 0

    async def send(self, _p: str, _c: Any, _m: str, url: str, **kwargs: Any) -> httpx.Response:
        assert url == "https://api.linear.app/graphql"
        query, variables = kwargs["json"]["query"], kwargs["json"]["variables"]
        if "GetComments" in query:
            return _response(
                {
                    "data": {
                        "issue": {
                            "comments": {
                                "nodes": [{"id": f"more-{i}"} for i in range(60)],
                                "pageInfo": {"hasNextPage": False},
                            }
                        }
                    }
                }
            )
        self.issue_pages += 1
        if self.issue_pages == self.fail_on:
            raise httpx.ConnectError("provider unavailable")
        since = poll_cursor.parse(variables["since"])
        matching = sorted(
            (i for i in self.issues if poll_cursor.parse(i["updatedAt"]) > since),
            key=lambda i: i["updatedAt"],
            reverse=True,
        )
        ids = [i["id"] for i in matching]
        if variables.get("last"):
            if variables.get("before"):
                matching = matching[: ids.index(variables["before"])]
            nodes = matching[-variables["last"] :]
            info = {"hasPreviousPage": len(matching) > len(nodes), "hasNextPage": False}
        else:
            if variables.get("after"):
                matching = matching[ids.index(variables["after"]) + 1 :]
            nodes = matching[: variables["first"]]
            info = {"hasNextPage": len(matching) > len(nodes), "hasPreviousPage": False}
        if nodes:
            info.update(startCursor=nodes[0]["id"], endCursor=nodes[-1]["id"])
        return _response({"data": {"issues": {"nodes": nodes, "pageInfo": info}}})


class Adapter:
    def __init__(self, **_kwargs: Any) -> None:
        pass

    def parse_issue_to_document(self, issue: dict[str, Any]) -> str:
        return str(issue.get("identifier") or issue.get("key"))

    async def fetch_issue(self, key: str) -> dict[str, Any]:
        return {"key": key}

    async def _get_client(self) -> Any:
        return MagicMock()


async def _poll_linear(provider: FakeLinear, store: Store, bus: Bus) -> int:
    with (
        patch.object(linear.http_retry, "send", provider.send),
        patch.object(linear, "LinearAdapter", Adapter),
        patch.object(poll_cursor, "save", store.save),
    ):
        try:
            return await linear._poll_organization(MagicMock(), bus, store.row, LOG)
        except httpx.ConnectError:
            return -1


def _linear_store() -> Store:
    return Store(linear_organization_id="org", access_token="t")


def _expected(prefix: str) -> list[str]:
    return [f"{prefix}{n}" for n in range(ITEMS)]


@pytest.mark.asyncio
async def test_linear_publishes_every_issue_once_oldest_first() -> None:
    provider, store, bus = FakeLinear(), _linear_store(), Bus()
    assert await _poll_linear(provider, store, bus) == ITEMS
    assert bus.published == _expected("ENG-")
    assert provider.issue_pages == 3
    assert store.row.poll_cursor == poll_cursor.parse(provider.issues[-1]["updatedAt"])
    assert await _poll_linear(provider, store, bus) == 0


@pytest.mark.asyncio
async def test_linear_pages_by_cursor_through_issues_updated_at_the_same_moment() -> None:
    provider, store, bus = FakeLinear(), _linear_store(), Bus()
    same = provider.issues[0]["updatedAt"]
    for issue in provider.issues:
        issue["updatedAt"] = same
    assert await _poll_linear(provider, store, bus) == ITEMS
    assert sorted(bus.published) == sorted(_expected("ENG-"))


@pytest.mark.asyncio
async def test_linear_reads_every_comment_page() -> None:
    provider, store = FakeLinear(), _linear_store()
    captured: list[dict[str, Any]] = []

    def capture(_adapter: Any, _tenant: Any, issue: dict[str, Any], *_rest: Any) -> list[str]:
        captured.append(issue)
        return [str(issue["identifier"])]

    with (
        patch.object(linear.http_retry, "send", provider.send),
        patch.object(linear, "LinearAdapter", Adapter),
        patch.object(linear, "issue_events", capture),
        patch.object(poll_cursor, "save", store.save),
    ):
        await linear._poll_organization(MagicMock(), Bus(), store.row, LOG)
    seventh = next(i for i in captured if i["identifier"] == "ENG-7")
    assert len(seventh["comments"]["nodes"]) == 61


@pytest.mark.asyncio
async def test_linear_resumes_after_a_failed_page() -> None:
    provider, store, bus = FakeLinear(), _linear_store(), Bus()
    provider.fail_on = 2
    assert await _poll_linear(provider, store, bus) == -1
    assert bus.published == _expected("ENG-")[:50]
    await _poll_linear(provider, store, bus)
    assert bus.published == _expected("ENG-")


@pytest.mark.asyncio
async def test_linear_page_cap_carries_over(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(poll_cursor, "MAX_PAGES", 2)
    provider, store, bus = FakeLinear(), _linear_store(), Bus()
    assert await _poll_linear(provider, store, bus) == 100
    assert await _poll_linear(provider, store, bus) == 20
    assert bus.published == _expected("ENG-")


@pytest.mark.asyncio
async def test_linear_first_poll_looks_back_a_week() -> None:
    provider, store = FakeLinear(), _linear_store()
    provider.issues = [
        {**provider.issues[0], "updatedAt": (NOW - timedelta(days=8)).isoformat()},
        {**provider.issues[1], "updatedAt": (NOW - timedelta(days=6)).isoformat()},
    ]
    bus = Bus()
    await _poll_linear(provider, store, bus)
    assert bus.published == ["ENG-1"]


@pytest.mark.asyncio
async def test_linear_stops_at_a_failed_publish_and_keeps_its_place() -> None:
    provider, store = FakeLinear(), _linear_store()

    class Flaky(Bus):
        async def publish(self, topic: str, event: Any) -> None:
            if event == "ENG-30" and not getattr(self, "failed", False):
                self.failed = True
                raise ConnectionError("bus down")
            await super().publish(topic, event)

    bus = Flaky()
    with pytest.raises(ConnectionError):
        with (
            patch.object(linear.http_retry, "send", provider.send),
            patch.object(linear, "LinearAdapter", Adapter),
            patch.object(poll_cursor, "save", store.save),
        ):
            await linear._poll_organization(MagicMock(), bus, store.row, LOG)
    await _poll_linear(provider, store, bus)
    assert bus.published == _expected("ENG-")


class FakeJira:
    """JQL search ordered by updated ascending, paged with startAt."""

    def __init__(self) -> None:
        self.issues = [
            {
                "key": f"ENG-{n}",
                "fields": {
                    "updated": (NOW - timedelta(hours=2) + timedelta(seconds=n)).strftime(
                        "%Y-%m-%dT%H:%M:%S.000+0000"
                    )
                },
            }
            for n in range(ITEMS)
        ]
        self.fail_on: int | None = None
        self.pages = 0

    async def send(self, _p: str, _c: Any, _m: str, url: str, **kwargs: Any) -> httpx.Response:
        assert url == "https://acme.atlassian.net/rest/api/3/search"
        self.pages += 1
        if self.pages == self.fail_on:
            raise httpx.ConnectError("provider unavailable")
        params = kwargs["params"]
        minutes = int(params["jql"].split('"-')[1].split("m")[0])
        window = NOW - timedelta(minutes=minutes + 1)
        matching = [i for i in self.issues if jira._updated(i) >= window]
        start, size = params["startAt"], params["maxResults"]
        return _response({"issues": matching[start : start + size], "total": len(matching)})


async def _poll_jira(provider: FakeJira, store: Store, bus: Bus) -> int:
    with (
        patch.object(jira.http_retry, "send", provider.send),
        patch.object(jira, "JiraAdapter", Adapter),
        patch.object(poll_cursor, "save", store.save),
    ):
        try:
            return await jira._poll_installation(MagicMock(), bus, store.row, LOG)
        except httpx.ConnectError:
            return -1


def _jira_store() -> Store:
    return Store(jira_host="acme.atlassian.net", personal_access_token="t")


@pytest.mark.asyncio
async def test_jira_publishes_every_issue_once_oldest_first() -> None:
    provider, store, bus = FakeJira(), _jira_store(), Bus()
    assert await _poll_jira(provider, store, bus) == ITEMS
    assert bus.published == _expected("ENG-")
    assert await _poll_jira(provider, store, bus) == 0


@pytest.mark.asyncio
async def test_jira_resumes_after_a_failed_page() -> None:
    provider, store, bus = FakeJira(), _jira_store(), Bus()
    provider.fail_on = 2
    assert await _poll_jira(provider, store, bus) == -1
    assert bus.published == _expected("ENG-")[:50]
    await _poll_jira(provider, store, bus)
    assert bus.published == _expected("ENG-")


@pytest.mark.asyncio
async def test_jira_page_cap_carries_over(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(poll_cursor, "MAX_PAGES", 2)
    provider, store, bus = FakeJira(), _jira_store(), Bus()
    assert await _poll_jira(provider, store, bus) == 100
    assert await _poll_jira(provider, store, bus) == 20
    assert bus.published == _expected("ENG-")


class FakeNotion:
    """Search sorted by last_edited_time ascending, paged with opaque cursors."""

    def __init__(self) -> None:
        self.pages = [
            {
                "id": f"page-{n}",
                "last_edited_time": (NOW - timedelta(days=1) + timedelta(minutes=n)).strftime(
                    "%Y-%m-%dT%H:%M:00.000Z"
                ),
            }
            for n in range(ITEMS)
        ]
        self.fail_on: int | None = None
        self.requests = 0

    async def search(
        self, _token: str, start_cursor: str | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        self.requests += 1
        if self.requests == self.fail_on:
            raise httpx.ConnectError("provider unavailable")
        start = int(start_cursor or 0)
        end = start + notion.PAGE_SIZE
        return self.pages[start:end], str(end) if end < len(self.pages) else None


class NotionPages:
    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def fetch_page_blocks(self, page_id: str) -> list[Any]:
        return []

    def parse_page_to_document(self, page_id: str, **_kwargs: Any) -> str:
        return page_id


async def _poll_notion(provider: FakeNotion, store: Store, bus: Bus) -> int:
    with (
        patch.object(notion, "_search_page", provider.search),
        patch.object(notion, "NotionAdapter", NotionPages),
        patch.object(poll_cursor, "save", store.save),
    ):
        try:
            return await notion._poll_workspace(MagicMock(), bus, store.row, LOG)
        except httpx.ConnectError:
            return -1


def _notion_store() -> Store:
    return Store(notion_workspace_id="ws", access_token="t")


@pytest.fixture
def notion_pages_of_fifty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notion, "PAGE_SIZE", 50)


@pytest.mark.asyncio
@pytest.mark.usefixtures("notion_pages_of_fifty")
async def test_notion_publishes_every_page_once_oldest_first() -> None:
    provider, store, bus = FakeNotion(), _notion_store(), Bus()
    assert await _poll_notion(provider, store, bus) == ITEMS
    assert bus.published == _expected("page-")
    assert provider.requests == 3
    assert store.row.poll_resume is None
    assert await _poll_notion(provider, store, bus) == 0


@pytest.mark.asyncio
@pytest.mark.usefixtures("notion_pages_of_fifty")
async def test_notion_resumes_after_a_failed_page() -> None:
    provider, store, bus = FakeNotion(), _notion_store(), Bus()
    provider.fail_on = 2
    assert await _poll_notion(provider, store, bus) == -1
    assert bus.published == _expected("page-")[:50]
    assert store.row.poll_resume == "50"
    await _poll_notion(provider, store, bus)
    assert bus.published == _expected("page-")


@pytest.mark.asyncio
@pytest.mark.usefixtures("notion_pages_of_fifty")
async def test_notion_page_cap_carries_over(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(poll_cursor, "MAX_PAGES", 2)
    provider, store, bus = FakeNotion(), _notion_store(), Bus()
    assert await _poll_notion(provider, store, bus) == 100
    assert store.row.poll_resume == "100"
    assert await _poll_notion(provider, store, bus) == 20
    assert bus.published == _expected("page-")


@pytest.mark.asyncio
@pytest.mark.usefixtures("notion_pages_of_fifty")
async def test_notion_leaves_pages_edited_this_minute_for_the_next_poll() -> None:
    provider, store, bus = FakeNotion(), _notion_store(), Bus()
    recent = NOW.strftime("%Y-%m-%dT%H:%M:00.000Z")
    provider.pages = [*provider.pages[:3], {"id": "page-recent", "last_edited_time": recent}]
    await _poll_notion(provider, store, bus)
    assert bus.published == _expected("page-")[:3]


@pytest.mark.asyncio
@pytest.mark.usefixtures("notion_pages_of_fifty")
async def test_notion_starts_over_when_its_saved_place_is_refused() -> None:
    provider, store, bus = FakeNotion(), _notion_store(), Bus()
    store.row.poll_resume = "stale"
    real: Callable[..., Any] = provider.search

    async def search(token: str, start_cursor: str | None) -> Any:
        if start_cursor == "stale":
            request = httpx.Request("POST", "https://api.notion.com/v1/search")
            raise httpx.HTTPStatusError(
                "bad cursor", request=request, response=httpx.Response(400, request=request)
            )
        return await real(token, start_cursor)

    provider.search = search  # type: ignore[method-assign]
    await _poll_notion(provider, store, bus)
    assert bus.published == _expected("page-")


def test_linear_reads_up_to_a_thousand_issues_per_poll() -> None:
    assert poll_cursor.MAX_PAGES * linear.PAGE_SIZE == 1000

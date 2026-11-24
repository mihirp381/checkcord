"""
Integration tests for the GitHub webhook endpoint.

Strategy
--------
* We use ``TestClient(app)`` as a context manager per-test (mirroring
  ``tests/api/test_push.py``) so that the FastAPI lifespan is fully started
  and torn down around each test.
* The webhook endpoint publishes ``CanonicalEvent`` objects to the Redis
  stream ``events.raw.code`` (or ``events.raw.monitoring`` for CI_PIPELINE/
  workflow_run events); it does NOT write to Postgres. Therefore we validate
  the event shape by reading from the Redis stream rather than from the
  database.
* Expensive REST calls (``get_pull_request_files``, ``get_commit_details``,
  ``get_collaborators``) are patched out so that tests remain hermetic and
  fast.
* Database fixtures (GithubInstallation rows) are created/torn-down around
  each test to guarantee isolation.
"""

import hashlib
import hmac
import json
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pubsub_test_helpers import assert_topic_not_dlqd, real_subscription, wait_for_message_in_stream
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.models.event import CanonicalEvent
from checkcord.pipeline.enricher import PipelineEnricher

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_NAME = "test_org/test_repo"
WEBHOOK_SECRET = "test_webhook_secret"
PAT = "ghp_webhook_test_token_value"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signature(payload_bytes: bytes, secret: str = WEBHOOK_SECRET) -> str:
    """Return the ``sha256=…`` HMAC digest for a payload."""
    digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _headers(payload_bytes: bytes, event_type: str, secret: str = WEBHOOK_SECRET) -> dict[str, str]:
    return {
        "X-GitHub-Event": event_type,
        "X-Hub-Signature-256": _make_signature(payload_bytes, secret),
    }


async def _drain_redis_stream(
    redis_url: str, stream_name: str = "events.raw.code"
) -> list[dict[str, Any]]:
    """Read all messages from the specified Redis stream and return decoded event dicts.

    The Redis stream stores ``CanonicalEvent`` as flat string fields (one per
    model attribute).  We use ``CanonicalEvent.from_redis_dict`` to reconstruct
    the model and then call ``model_dump`` to get a plain dict for assertions.
    """
    client = Redis.from_url(redis_url)
    try:
        raw_streams: Any = await client.xread(streams={stream_name: "0-0"}, count=100)
        events: list[dict[str, Any]] = []
        if raw_streams:
            for _, messages in raw_streams:
                for _msg_id, fields in messages:
                    # Decode bytes → str for all field keys and values
                    decoded = {
                        (k.decode("utf-8") if isinstance(k, bytes) else k): (
                            v.decode("utf-8") if isinstance(v, bytes) else v
                        )
                        for k, v in fields.items()
                    }
                    event = CanonicalEvent.from_redis_dict(decoded)
                    events.append(event.model_dump(mode="json"))
        return events
    finally:
        await client.aclose()


async def _drain_raw_redis_stream(redis_url: str, stream_name: str) -> list[dict[str, Any]]:
    """Read all raw messages from specified Redis stream as simple dicts."""
    client = Redis.from_url(redis_url)
    try:
        raw_streams: Any = await client.xread(streams={stream_name: "0-0"}, count=100)
        events: list[dict[str, Any]] = []
        if raw_streams:
            for _, messages in raw_streams:
                for _msg_id, fields in messages:
                    decoded = {
                        (k.decode("utf-8") if isinstance(k, bytes) else k): (
                            v.decode("utf-8") if isinstance(v, bytes) else v
                        )
                        for k, v in fields.items()
                    }
                    events.append(decoded)
        return events
    finally:
        await client.aclose()


async def _flush_redis_stream(redis_url: str, stream_name: str = "events.raw.code") -> None:
    client = Redis.from_url(redis_url)
    try:
        await client.delete(stream_name)
    except Exception:
        pass
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def github_installation() -> Any:
    """
    Insert a GithubInstallation row (plus supporting EnterpriseUser and
    ChatUser rows) before each test and remove everything after.

    The ``autouse=True`` means every test in this module gets a fresh, isolated
    installation row without having to declare the fixture explicitly.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    tenant_id = uuid4()
    ent_id = uuid4()

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text(
                "INSERT INTO enterprise_users "
                "(id, tenant_id, primary_email, display_name, active) "
                "VALUES (:id, :tenant_id, 'coder@example.com', 'GitHub Coder', True)"
            ),
            {"id": ent_id, "tenant_id": tenant_id},
        )
        await conn.execute(
            text(
                "INSERT INTO chat_users "
                "(id, tenant_id, platform, native_id, display_name, email, internal_id) "
                "VALUES (:id, :tenant_id, 'github', 'test_coder', "
                "'GitHub Coder', 'coder@example.com', :internal_id)"
            ),
            {"id": uuid4(), "tenant_id": tenant_id, "internal_id": ent_id},
        )
        await conn.execute(
            text(
                "INSERT INTO github_installations "
                "(id, repo_identifier, tenant_id, personal_access_token, webhook_secret) "
                "VALUES (:id, :repo, :tenant_id, :pat, :secret)"
            ),
            {
                "id": uuid4(),
                "repo": REPO_NAME,
                "tenant_id": tenant_id,
                "pat": PAT,
                "secret": WEBHOOK_SECRET,
            },
        )

    yield {"tenant_id": tenant_id, "ent_id": ent_id}

    # Teardown
    await _flush_redis_stream(settings.redis_url, "events.raw.code")
    await _flush_redis_stream(settings.redis_url, "events.raw.monitoring")
    await _flush_redis_stream(settings.redis_url, "ci.pr.evaluate")
    await _flush_redis_stream(settings.redis_url, "ci.pr.merged")
    await _flush_redis_stream(settings.redis_url, "ci.pr.fallback_zset")
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text("DELETE FROM github_installations WHERE repo_identifier = :repo"),
            {"repo": REPO_NAME},
        )
        await conn.execute(
            text("DELETE FROM chat_users WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
        await conn.execute(
            text("DELETE FROM enterprise_users WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
    await engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_ping(github_installation: dict[str, Any]) -> None:
    """GitHub sends a ``ping`` event right after a webhook is registered."""
    payload = {
        "zen": "Keep it simple, stupid.",
        "hook_id": 123456,
        "repository": {"full_name": REPO_NAME, "private": True},
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes, "ping")

    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/github", content=payload_bytes, headers=hdrs)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_webhook_invalid_signature(github_installation: dict[str, Any]) -> None:
    """Requests with a forged signature must be rejected with 401."""
    payload = {"repository": {"full_name": REPO_NAME, "private": True}}
    payload_bytes = json.dumps(payload).encode()

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/webhooks/github",
            content=payload_bytes,
            headers={
                "X-GitHub-Event": "ping",
                "X-Hub-Signature-256": "sha256=deadbeef",
            },
        )

    assert response.status_code == 401
    assert "Invalid signature" in response.json()["detail"]


@pytest.mark.asyncio
async def test_webhook_unknown_repo(github_installation: dict[str, Any]) -> None:
    """Webhooks for repositories not registered must return 401."""
    payload = {
        "action": "opened",
        "repository": {"full_name": "unknown_org/unknown_repo", "private": True},
    }
    payload_bytes = json.dumps(payload).encode()
    # Signed with our secret, but the repo won't be found in DB
    hdrs = _headers(payload_bytes, "pull_request")

    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/github", content=payload_bytes, headers=hdrs)

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_webhook_pr_opened(github_installation: dict[str, Any]) -> None:
    """
    A ``pull_request`` webhook with ``action=opened`` must:
    1. Return 202 Accepted.
    2. Publish a ``CanonicalEvent`` to the Redis stream with the correct
       PR metadata and author information.
    """
    settings = get_settings()
    payload = {
        "action": "opened",
        "number": 42,
        "pull_request": {
            "number": 42,
            "title": "Fix Auth Bug",
            "body": "Fixes auth redirection",
            "created_at": "2026-06-24T05:00:00Z",
            "html_url": "https://github.com/test_org/test_repo/pull/42",
            "user": {"login": "test_coder", "email": "coder@example.com"},
            "head": {"ref": "auth-fix", "sha": "sha_head"},
            "base": {"ref": "main"},
        },
        "repository": {"full_name": REPO_NAME, "private": True},
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes, "pull_request")

    with (
        patch(
            "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.get_pull_request_files",
            new_callable=AsyncMock,
            return_value=(["src/auth.py"], 10, 2),
        ),
        patch(
            "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.get_collaborators",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        with TestClient(app) as client:
            response = client.post("/api/v1/webhooks/github", content=payload_bytes, headers=hdrs)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "processed"
    assert len(body["event_ids"]) == 1

    # Verify the event was published to the Redis stream
    events = await _drain_redis_stream(settings.redis_url)
    assert len(events) == 1, f"Expected 1 event in stream, got {len(events)}"
    event = events[0]

    assert event["source_platform"] == "github"
    assert event["source_type"] == "pr_opened"
    code_meta = event["code_metadata"]
    assert code_meta["repository"] == REPO_NAME
    assert code_meta["files_changed"] == ["src/auth.py"]
    assert code_meta["lines_added"] == 10
    assert code_meta["lines_removed"] == 2
    assert code_meta["pull_request_id"] == "42"
    assert event["author"]["source_id"] == "test_coder"


@pytest.mark.asyncio
async def test_webhook_pr_opened_is_actually_consumed_by_enricher(
    github_installation: dict[str, Any],
) -> None:
    """PR, push and review events must reach a real consumer.

    They were once published to events.raw.chat, which has no CanonicalEvent
    consumer, and were lost. This starts a real PipelineEnricher subscribed as
    checkcord/worker.py subscribes it (events.raw.code, no mocked
    bus.subscribe) and asserts it receives and re-publishes the event.
    """
    settings = get_settings()
    payload = {
        "action": "opened",
        "number": 43,
        "pull_request": {
            "number": 43,
            "title": "Fix Auth Bug 2",
            "body": "Fixes auth redirection again",
            "created_at": "2026-06-24T05:00:00Z",
            "html_url": "https://github.com/test_org/test_repo/pull/43",
            "user": {"login": "test_coder", "email": "coder@example.com"},
            "head": {"ref": "auth-fix-2", "sha": "sha_head_2"},
            "base": {"ref": "main"},
        },
        "repository": {"full_name": REPO_NAME, "private": True},
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes, "pull_request")

    with (
        patch(
            "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.get_pull_request_files",
            new_callable=AsyncMock,
            return_value=(["src/auth.py"], 10, 2),
        ),
        patch(
            "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.get_collaborators",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        with TestClient(app) as client:
            response = client.post("/api/v1/webhooks/github", content=payload_bytes, headers=hdrs)

    assert response.status_code == 202, response.text
    event_id = response.json()["event_ids"][0]

    redis_client = Redis.from_url(settings.redis_url)
    engine = create_async_engine(settings.database_url)
    enricher = PipelineEnricher(
        bus=RedisStreamsEventBus(redis_client),
        engine=engine,
        group_name="test_enricher_code_e2e",
        consumer_name="enricher-code-e2e-1",
    )

    try:
        async with real_subscription(lambda: enricher._subscribe_to_topic("events.raw.code")):
            found = await wait_for_message_in_stream(
                redis_client,
                "events.enriched.chat",
                lambda fields: fields.get("event_id") == event_id,
            )

        assert found, "GitHub PR event was never enriched and republished to events.enriched.chat"

        await assert_topic_not_dlqd(redis_client, "events.raw.code")
    finally:
        await redis_client.delete("events.enriched.chat", "events.raw.code.dlq")
        await redis_client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_webhook_pr_review_submitted(github_installation: dict[str, Any]) -> None:
    """
    A ``pull_request_review`` webhook with ``action=submitted`` must produce
    a ``pr_review`` CanonicalEvent in the Redis stream.
    """
    settings = get_settings()
    payload = {
        "action": "submitted",
        "review": {
            "id": 999,
            "body": "Looks good to me!",
            "state": "APPROVED",
            "submitted_at": "2026-06-24T05:30:00Z",
            "user": {"login": "test_coder", "email": "coder@example.com"},
        },
        "pull_request": {
            "number": 42,
            "title": "Fix Auth Bug",
            "body": None,
            "created_at": "2026-06-24T05:00:00Z",
            "html_url": "https://github.com/test_org/test_repo/pull/42",
            "user": {"login": "test_coder", "email": "coder@example.com"},
            "head": {"ref": "auth-fix", "sha": "sha_head"},
            "base": {"ref": "main"},
        },
        "repository": {"full_name": REPO_NAME, "private": True},
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes, "pull_request_review")

    with patch(
        "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.get_collaborators",
        new_callable=AsyncMock,
        return_value=[],
    ):
        with TestClient(app) as client:
            response = client.post("/api/v1/webhooks/github", content=payload_bytes, headers=hdrs)

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "processed"
    assert len(response.json()["event_ids"]) == 1

    events = await _drain_redis_stream(settings.redis_url)
    assert len(events) == 1
    event = events[0]
    assert event["source_platform"] == "github"
    assert event["source_type"] == "pr_review"
    assert "APPROVED" in event["content_raw"]


@pytest.mark.asyncio
async def test_webhook_pr_review_comment(github_installation: dict[str, Any]) -> None:
    """
    ``pull_request_review_comment`` events with ``action=created`` must produce
    a ``pr_comment`` CanonicalEvent in the stream.
    """
    settings = get_settings()
    payload = {
        "action": "created",
        "comment": {
            "id": 777,
            "body": "nit: rename this variable",
            "created_at": "2026-06-24T05:45:00Z",
            "user": {"login": "test_coder", "email": "coder@example.com"},
        },
        "pull_request": {
            "number": 42,
            "title": "Fix Auth Bug",
            "body": None,
            "created_at": "2026-06-24T05:00:00Z",
            "html_url": "https://github.com/test_org/test_repo/pull/42",
            "user": {"login": "test_coder", "email": "coder@example.com"},
            "head": {"ref": "auth-fix", "sha": "sha_head"},
            "base": {"ref": "main"},
        },
        "repository": {"full_name": REPO_NAME, "private": True},
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes, "pull_request_review_comment")

    with patch(
        "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.get_collaborators",
        new_callable=AsyncMock,
        return_value=[],
    ):
        with TestClient(app) as client:
            response = client.post("/api/v1/webhooks/github", content=payload_bytes, headers=hdrs)

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "processed"

    events = await _drain_redis_stream(settings.redis_url)
    assert len(events) == 1
    event = events[0]
    assert event["source_type"] == "pr_comment"
    assert "777" in event["source_id"]


@pytest.mark.asyncio
async def test_webhook_push(github_installation: dict[str, Any]) -> None:
    """
    A ``push`` webhook must produce one ``commit`` CanonicalEvent per commit in
    the Redis stream, with correct file-level metadata.
    """
    settings = get_settings()
    payload = {
        "ref": "refs/heads/main",
        "before": "sha_before",
        "after": "sha_after",
        "commits": [
            {
                "id": "commit_sha_1",
                "message": "fix: update db schema",
                "timestamp": "2026-06-24T05:05:00Z",
                "author": {
                    "name": "GitHub Coder",
                    "email": "coder@example.com",
                    "username": "test_coder",
                },
                "added": ["src/db.py"],
                "removed": [],
                "modified": ["README.md"],
            }
        ],
        "repository": {"full_name": REPO_NAME, "private": True},
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes, "push")

    with (
        patch(
            "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.get_commit_details",
            new_callable=AsyncMock,
            return_value=(["src/db.py", "README.md"], 50, 10),
        ),
        patch(
            "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.get_collaborators",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        with TestClient(app) as client:
            response = client.post("/api/v1/webhooks/github", content=payload_bytes, headers=hdrs)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "processed"
    assert len(body["event_ids"]) == 1

    events = await _drain_redis_stream(settings.redis_url)
    assert len(events) == 1
    event = events[0]
    assert event["source_platform"] == "github"
    assert event["source_type"] == "commit"
    code_meta = event["code_metadata"]
    assert code_meta["commit_sha"] == "commit_sha_1"
    assert set(code_meta["files_changed"]) == {"src/db.py", "README.md"}
    assert code_meta["lines_added"] == 50
    assert code_meta["lines_removed"] == 10
    assert event["author"]["source_id"] == "test_coder"


@pytest.mark.asyncio
async def test_webhook_push_100_file_cap(github_installation: dict[str, Any]) -> None:
    """
    A ``push`` event where the REST client returns > 100 files must be capped
    at exactly 100 files in ``CodeMetadata.files_changed``.

    *Why:* Large refactors could produce unbounded payloads that overwhelm
    JSONB serialisation.  The 100-file cap is the agreed safety guardrail.
    """
    settings = get_settings()
    many_files = [f"src/module_{i}.py" for i in range(150)]

    payload = {
        "ref": "refs/heads/main",
        "before": "sha_before",
        "after": "sha_after",
        "commits": [
            {
                "id": "big_refactor_sha",
                "message": "refactor: large cleanup",
                "timestamp": "2026-06-24T06:00:00Z",
                "author": {
                    "name": "GitHub Coder",
                    "email": "coder@example.com",
                    "username": "test_coder",
                },
                "added": [],
                "removed": [],
                "modified": many_files[:50],
            }
        ],
        "repository": {"full_name": REPO_NAME, "private": True},
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes, "push")

    with (
        patch(
            "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.get_commit_details",
            new_callable=AsyncMock,
            return_value=(many_files, 5000, 100),  # 150 files from REST
        ),
        patch(
            "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.get_collaborators",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        with TestClient(app) as client:
            response = client.post("/api/v1/webhooks/github", content=payload_bytes, headers=hdrs)

    assert response.status_code == 202, response.text

    events = await _drain_redis_stream(settings.redis_url)
    assert len(events) == 1
    code_meta = events[0]["code_metadata"]
    assert len(code_meta["files_changed"]) == 100, (
        f"Expected files_changed capped at 100, got {len(code_meta['files_changed'])}"
    )


@pytest.mark.asyncio
async def test_webhook_ignored_event_type(github_installation: dict[str, Any]) -> None:
    """
    Unsupported event types (e.g. ``star``) must be acknowledged with a 200
    ``ignored`` response and must NOT publish any event to the stream.
    """
    settings = get_settings()
    payload = {
        "action": "created",
        "repository": {"full_name": REPO_NAME, "private": True},
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes, "star")

    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/github", content=payload_bytes, headers=hdrs)

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"

    events = await _drain_redis_stream(settings.redis_url)
    assert len(events) == 0, f"Expected 0 events for ignored type, got {len(events)}"


@pytest.mark.asyncio
async def test_webhook_workflow_run_failure(github_installation: dict[str, Any]) -> None:
    """
    A completed workflow_run event with a 'failure' conclusion must be ingested
    and published to the dedicated monitoring stream.
    """
    settings = get_settings()
    payload = {
        "action": "completed",
        "workflow_run": {
            "id": 8888,
            "name": "CI Run Suite",
            "head_branch": "main",
            "head_sha": "abcdef123456",
            "status": "completed",
            "conclusion": "failure",
            "html_url": "https://github.com/test_org/test_repo/actions/runs/8888",
            "created_at": "2026-06-24T12:00:00Z",
            "updated_at": "2026-06-24T12:05:00Z",
        },
        "repository": {"full_name": REPO_NAME, "private": True},
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes, "workflow_run")

    from checkcord.models.cicd import CIFailureContent

    with (
        patch(
            "checkcord.adapters.code.providers.github.parser.get_repository_acl",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "checkcord.adapters.cicd.providers.github.webhook._cicd_provider.fetch_failure_content",
            new_callable=AsyncMock,
            return_value=CIFailureContent(source="unavailable"),
        ),
        patch(
            "checkcord.adapters.cicd.providers.github.webhook.link_ci_failure_to_contexts",
            new_callable=AsyncMock,
            return_value=0,
        ),
        TestClient(app) as client,
    ):
        response = client.post("/api/v1/webhooks/github", content=payload_bytes, headers=hdrs)

    assert response.status_code == 202
    assert response.json()["status"] == "processed"

    # Drain from the dedicated monitoring stream
    events = await _drain_redis_stream(settings.redis_url, "events.raw.monitoring")
    assert len(events) == 1
    event = events[0]
    assert event["source_platform"] == "github"
    assert event["source_type"] == "ci_pipeline"
    assert event["event_action"] == "created"
    assert event["source_id"] == "github:workflow_run:8888"
    assert "CI Run Suite" in event["content_raw"]
    assert "failure" in event["content_raw"]


@pytest.mark.asyncio
async def test_webhook_workflow_run_success_ignored(github_installation: dict[str, Any]) -> None:
    """
    A completed workflow_run event with a 'success' conclusion must be ignored.
    """
    settings = get_settings()
    payload = {
        "action": "completed",
        "workflow_run": {
            "id": 8889,
            "name": "CI Run Suite",
            "head_branch": "main",
            "head_sha": "abcdef123456",
            "status": "completed",
            "conclusion": "success",
            "html_url": "https://github.com/test_org/test_repo/actions/runs/8889",
            "created_at": "2026-06-24T12:00:00Z",
            "updated_at": "2026-06-24T12:05:00Z",
        },
        "repository": {"full_name": REPO_NAME, "private": True},
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes, "workflow_run")

    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/github", content=payload_bytes, headers=hdrs)

    assert response.status_code == 202
    assert response.json()["status"] == "ignored_run"

    # Verify no events are published to events.raw.monitoring
    events = await _drain_redis_stream(settings.redis_url, "events.raw.monitoring")
    assert len(events) == 0


@pytest.mark.asyncio
async def test_webhook_pr_opened_and_synchronize_ci_stream(
    github_installation: dict[str, Any],
) -> None:
    """
    A ``pull_request`` webhook with ``action=opened`` or ``action=synchronize``
    must publish the required payload to the ``ci.pr.evaluate`` Redis stream.
    """
    settings = get_settings()

    # 1. Test opened action
    payload_opened = {
        "action": "opened",
        "number": 42,
        "pull_request": {
            "number": 42,
            "title": "Fix Auth Bug",
            "body": "Fixes auth redirection",
            "created_at": "2026-06-24T05:00:00Z",
            "html_url": "https://github.com/test_org/test_repo/pull/42",
            "user": {"login": "test_coder", "email": "coder@example.com"},
            "head": {"ref": "auth-fix", "sha": "sha_head_opened"},
            "base": {"ref": "main"},
        },
        "repository": {"full_name": REPO_NAME, "private": True},
    }
    payload_bytes_opened = json.dumps(payload_opened).encode()
    hdrs_opened = _headers(payload_bytes_opened, "pull_request")

    # 2. Test synchronize action
    payload_sync = {
        "action": "synchronize",
        "number": 42,
        "pull_request": {
            "number": 42,
            "title": "Fix Auth Bug",
            "body": "Fixes auth redirection",
            "created_at": "2026-06-24T05:00:00Z",
            "html_url": "https://github.com/test_org/test_repo/pull/42",
            "user": {"login": "test_coder", "email": "coder@example.com"},
            "head": {"ref": "auth-fix", "sha": "sha_head_sync"},
            "base": {"ref": "main"},
        },
        "repository": {"full_name": REPO_NAME, "private": True},
    }
    payload_bytes_sync = json.dumps(payload_sync).encode()
    hdrs_sync = _headers(payload_bytes_sync, "pull_request")

    with (
        patch(
            "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.get_pull_request_files",
            new_callable=AsyncMock,
            return_value=(["src/auth.py"], 10, 2),
        ),
        patch(
            "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.get_collaborators",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        with TestClient(app) as client:
            resp_opened = client.post(
                "/api/v1/webhooks/github", content=payload_bytes_opened, headers=hdrs_opened
            )
            resp_sync = client.post(
                "/api/v1/webhooks/github", content=payload_bytes_sync, headers=hdrs_sync
            )

    assert resp_opened.status_code == 202
    assert resp_sync.status_code == 202

    # Drain from the dedicated ci.pr.evaluate stream
    events = await _drain_raw_redis_stream(settings.redis_url, "ci.pr.evaluate")
    assert len(events) == 2, f"Expected 2 events in stream, got {len(events)}"

    # Assert details of opened event
    event_opened = events[0]
    assert event_opened["repository_full_name"] == REPO_NAME
    assert event_opened["pull_request_number"] == "42"
    assert event_opened["head_sha"] == "sha_head_opened"
    assert event_opened["tenant_id"] == str(github_installation["tenant_id"])
    assert "personal_access_token" not in event_opened
    assert event_opened["action"] == "opened"

    # Assert details of synchronize event
    event_sync = events[1]
    assert event_sync["repository_full_name"] == REPO_NAME
    assert event_sync["pull_request_number"] == "42"
    assert event_sync["head_sha"] == "sha_head_sync"
    assert event_sync["tenant_id"] == str(github_installation["tenant_id"])
    assert "personal_access_token" not in event_sync
    assert event_sync["action"] == "synchronize"
    assert PAT not in json.dumps(events)

    redis = Redis.from_url(settings.redis_url)
    try:
        fallback = [m.decode() for m in await redis.zrange("ci.pr.fallback_zset", 0, -1)]
    finally:
        await redis.aclose()
    shas = {json.loads(m)["sha"] for m in fallback}
    assert {"sha_head_opened", "sha_head_sync"} <= shas
    assert all("pat" not in json.loads(m) for m in fallback)
    assert PAT not in "".join(fallback)


@pytest.mark.asyncio
async def test_webhook_pr_redis_failure(github_installation: dict[str, Any]) -> None:
    """
    If Redis stream publishing fails, the webhook endpoint must raise a 503
    HTTPException so GitHub can retry later.
    """
    payload = {
        "action": "opened",
        "number": 42,
        "pull_request": {
            "number": 42,
            "title": "Fix Auth Bug",
            "body": "Fixes auth redirection",
            "created_at": "2026-06-24T05:00:00Z",
            "html_url": "https://github.com/test_org/test_repo/pull/42",
            "user": {"login": "test_coder", "email": "coder@example.com"},
            "head": {"ref": "auth-fix", "sha": "sha_head_opened"},
            "base": {"ref": "main"},
        },
        "repository": {"full_name": REPO_NAME, "private": True},
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes, "pull_request")

    with (
        patch(
            "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.get_pull_request_files",
            new_callable=AsyncMock,
            return_value=(["src/auth.py"], 10, 2),
        ),
        patch(
            "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.get_collaborators",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "redis.asyncio.Redis.xadd",
            new_callable=AsyncMock,
            side_effect=Exception("Redis is down"),
        ),
    ):
        with TestClient(app) as client:
            response = client.post("/api/v1/webhooks/github", content=payload_bytes, headers=hdrs)

    assert response.status_code == 503
    assert "Redis stream unavailable" in response.json()["detail"]


@pytest.mark.asyncio
async def test_webhook_pr_ci_evaluate_xadd_does_not_trim(
    github_installation: dict[str, Any],
) -> None:
    """The worker trims acknowledged entries; a publish-time MAXLEN could drop unread ones."""

    captured_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    real_xadd = Redis.xadd

    async def spy_xadd(self: Redis, *args: Any, **kwargs: Any) -> Any:
        captured_calls.append((args, kwargs))
        return await real_xadd(self, *args, **kwargs)

    payload = {
        "action": "opened",
        "number": 42,
        "pull_request": {
            "number": 42,
            "title": "Fix Auth Bug",
            "body": "Fixes auth redirection",
            "created_at": "2026-06-24T05:00:00Z",
            "html_url": "https://github.com/test_org/test_repo/pull/42",
            "user": {"login": "test_coder", "email": "coder@example.com"},
            "head": {"ref": "auth-fix", "sha": "sha_maxlen_check"},
            "base": {"ref": "main"},
        },
        "repository": {"full_name": REPO_NAME, "private": True},
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes, "pull_request")

    with (
        patch(
            "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.get_pull_request_files",
            new_callable=AsyncMock,
            return_value=(["src/auth.py"], 10, 2),
        ),
        patch(
            "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.get_collaborators",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.create_commit_status",
            new_callable=AsyncMock,
        ) as create_status,
        patch("redis.asyncio.Redis.xadd", autospec=True, side_effect=spy_xadd),
    ):
        with TestClient(app) as client:
            resp = client.post("/api/v1/webhooks/github", content=payload_bytes, headers=hdrs)

    assert resp.status_code == 202
    create_status.assert_awaited_once()

    ci_pr_evaluate_calls = [c for c in captured_calls if c[0] and c[0][0] == "ci.pr.evaluate"]
    assert len(ci_pr_evaluate_calls) == 1
    _, call_kwargs = ci_pr_evaluate_calls[0]
    assert "maxlen" not in call_kwargs


# ---------------------------------------------------------------------------
# pull_request closed+merged must publish ci.pr.merged and set
# CodeMetadata.merge_commit=True; closed-without-merge and opened/synchronize
# must not publish ci.pr.merged.
# ---------------------------------------------------------------------------


def _merged_pr_payload(number: int = 42, merge_sha: str = "merge_sha_abc") -> dict[str, Any]:
    return {
        "action": "closed",
        "number": number,
        "pull_request": {
            "number": number,
            "title": "Fix Auth Bug",
            "body": "Fixes auth redirection",
            "created_at": "2026-06-24T05:00:00Z",
            "html_url": f"https://github.com/test_org/test_repo/pull/{number}",
            "user": {"login": "test_coder", "email": "coder@example.com"},
            "head": {"ref": "auth-fix", "sha": "sha_head"},
            "base": {"ref": "main"},
            "merged": True,
            "merge_commit_sha": merge_sha,
            "merged_at": "2026-06-24T06:00:00Z",
        },
        "repository": {"full_name": REPO_NAME, "private": True},
    }


@pytest.mark.asyncio
async def test_webhook_pr_merged_publishes_ci_pr_merged(
    github_installation: dict[str, Any],
) -> None:
    settings = get_settings()
    payload = _merged_pr_payload()
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes, "pull_request")

    with (
        patch(
            "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.get_pull_request_files",
            new_callable=AsyncMock,
            return_value=(["src/auth.py"], 10, 2),
        ),
        patch(
            "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.get_collaborators",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        with TestClient(app) as client:
            response = client.post("/api/v1/webhooks/github", content=payload_bytes, headers=hdrs)

    assert response.status_code == 202, response.text

    # 1. A CanonicalEvent with merge_commit=True lands on events.raw.code.
    events = await _drain_redis_stream(settings.redis_url)
    assert len(events) == 1
    event = events[0]
    assert event["source_type"] == "pr_opened"
    assert event["source_id"].endswith(":merged")
    assert "Merged" in event["content_raw"]
    code_meta = event["code_metadata"]
    assert code_meta["merge_commit"] is True
    assert code_meta["commit_sha"] == "merge_sha_abc"

    # 2. ci.pr.merged carries the merge commit SHA, not the head SHA.
    merged_events = await _drain_raw_redis_stream(settings.redis_url, "ci.pr.merged")
    assert len(merged_events) == 1
    merged_fields = merged_events[0]
    from checkcord.models.pr_merge import PRMergedPayload

    merged_payload = PRMergedPayload.model_validate_json(merged_fields["data"])
    assert merged_payload.repository_full_name == REPO_NAME
    assert merged_payload.pull_request_number == 42
    assert merged_payload.merge_commit_sha == "merge_sha_abc"
    assert merged_payload.tenant_id == github_installation["tenant_id"]
    assert "personal_access_token" not in json.loads(merged_fields["data"])
    assert PAT not in json.dumps(merged_fields)


@pytest.mark.asyncio
async def test_webhook_pr_closed_without_merge_no_ci_pr_merged(
    github_installation: dict[str, Any],
) -> None:
    """A PR closed WITHOUT being merged must not trigger the AST writer."""
    settings = get_settings()
    payload = _merged_pr_payload()
    payload["pull_request"]["merged"] = False
    payload["pull_request"]["merge_commit_sha"] = None
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes, "pull_request")

    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/github", content=payload_bytes, headers=hdrs)

    assert response.status_code == 202, response.text

    merged_events = await _drain_raw_redis_stream(settings.redis_url, "ci.pr.merged")
    assert len(merged_events) == 0

    # No CanonicalEvent either -- parse_pull_request_event returns None for
    # closed-without-merge, same as any other unhandled action.
    events = await _drain_redis_stream(settings.redis_url)
    assert len(events) == 0

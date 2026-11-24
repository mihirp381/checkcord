"""Integration tests for the agent trajectory API endpoint."""

import json
from collections.abc import AsyncGenerator, Iterator
from typing import cast
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from key_helpers import tenant_keys
from redis.asyncio import Redis

from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.models.event import CanonicalEvent

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def setup_agent_tokens() -> Iterator[dict[str, str]]:
    """Setup Bearer token for agent trajectory push tests."""
    tenant_id = str(uuid4())
    token = "token_agent_test"
    tokens = {token: tenant_id}
    with tenant_keys(tokens):
        yield tokens


@pytest_asyncio.fixture
async def cleanup_agent_stream() -> AsyncGenerator[None, None]:
    """Clean up the events.raw.agent Redis stream before and after tests."""
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        await redis.delete("events.raw.agent")
    except Exception:
        pass
    await redis.aclose()

    yield

    redis = Redis.from_url(settings.redis_url)
    try:
        await redis.delete("events.raw.agent")
    except Exception:
        pass
    await redis.aclose()


def test_agent_trajectory_auth_required() -> None:
    """Requests without Bearer token return 401."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/agents/trajectory",
            content=json.dumps({"raw_transcript": "test"}),
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 401


def test_agent_trajectory_invalid_token() -> None:
    """Requests with invalid Bearer token return 401."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/agents/trajectory",
            content=json.dumps({"raw_transcript": "test"}),
            headers={
                "Authorization": "Bearer bad_token_xyz",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 401


def test_agent_trajectory_missing_content() -> None:
    """Requests with neither steps nor raw_transcript return 422."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/agents/trajectory",
            content=json.dumps({"agent_name": "empty-agent"}),
            headers={
                "Authorization": "Bearer token_agent_test",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_agent_trajectory_structured_steps(
    cleanup_agent_stream: None,
) -> None:
    """POST with structured steps publishes to events.raw.agent stream."""
    payload = {
        "agent_name": "test-agent",
        "session_id": "sess-integration-001",
        "repository": "org/test-repo",
        "task_description": "Fix the login bug",
        "steps": [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "content": "Fix the authentication bug",
            },
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "Analyzing auth module...",
                "tool_calls": [
                    {
                        "tool_name": "view_file",
                        "arguments": {
                            "AbsolutePath": "/src/auth.py",
                        },
                    },
                    {
                        "tool_name": "replace_file_content",
                        "arguments": {
                            "TargetFile": "/src/login.py",
                        },
                    },
                ],
            },
        ],
    }

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/agents/trajectory",
            content=json.dumps(payload),
            headers={
                "Authorization": "Bearer token_agent_test",
                "Content-Type": "application/json",
            },
        )

    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert "event_id" in data
    assert "view_file" in data["tools_extracted"]
    assert "replace_file_content" in data["tools_extracted"]
    assert "/src/auth.py" in data["files_referenced"]
    assert "/src/login.py" in data["files_referenced"]

    # Verify event in Redis stream
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    streams = await redis.xread(streams={"events.raw.agent": 0}, count=100)
    await redis.aclose()

    assert streams, "No messages found in events.raw.agent stream"

    typed_streams = cast(
        list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]],
        streams,
    )
    message_found = False
    for _, messages in typed_streams:
        for _, fields in messages:
            decoded = {k.decode("utf-8"): v.decode("utf-8") for k, v in fields.items()}
            if decoded.get("event_id") == data["event_id"]:
                message_found = True
                event = CanonicalEvent.from_redis_dict(decoded)
                assert event.source_platform == "agent_session"
                assert event.source_type == "agent_log"
                assert "test-agent" in event.content_raw
                assert len(event.entities) == 2
                entity_ids = {e.entity_id for e in event.entities}
                assert "file:///src/auth.py" in entity_ids
                assert "file:///src/login.py" in entity_ids
                break

    assert message_found, f"Event {data['event_id']} not found in events.raw.agent"


@pytest.mark.asyncio
async def test_agent_trajectory_raw_transcript(
    cleanup_agent_stream: None,
) -> None:
    """POST with raw_transcript successfully ingests."""
    payload = {
        "agent_name": "markdown-agent",
        "raw_transcript": (
            "# Session Log\n"
            "Read /Users/dev/src/utils.py for helper functions.\n"
            "Modified /Users/dev/src/main.py to fix the issue.\n"
        ),
    }

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/agents/trajectory",
            content=json.dumps(payload),
            headers={
                "Authorization": "Bearer token_agent_test",
                "Content-Type": "application/json",
            },
        )

    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert len(data["files_referenced"]) >= 2


@pytest.mark.asyncio
async def test_agent_trajectory_raw_transcript_json_array(
    cleanup_agent_stream: None,
) -> None:
    """POST with raw_transcript containing a JSON array of steps successfully parses and ingests."""
    payload = {
        "agent_name": "json-array-agent",
        "raw_transcript": json.dumps(
            [
                {
                    "step_index": 0,
                    "tool_calls": [
                        {"tool_name": "view_file", "arguments": {"AbsolutePath": "/src/config.py"}}
                    ],
                }
            ]
        ),
    }

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/agents/trajectory",
            content=json.dumps(payload),
            headers={
                "Authorization": "Bearer token_agent_test",
                "Content-Type": "application/json",
            },
        )

    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert "/src/config.py" in data["files_referenced"]

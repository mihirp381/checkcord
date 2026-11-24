import hashlib
import hmac
import json
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from checkcord.core.config import get_settings
from checkcord.main import app
from checkcord.models.event import CanonicalEvent

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

REPO_NAME = "test_org/test_repo"
WEBHOOK_SECRET = "test_webhook_secret"
PAT = "test_pat"


def _make_signature(payload_bytes: bytes, secret: str = WEBHOOK_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _headers(payload_bytes: bytes, event_type: str, secret: str = WEBHOOK_SECRET) -> dict[str, str]:
    return {
        "X-GitHub-Event": event_type,
        "X-Hub-Signature-256": _make_signature(payload_bytes, secret),
    }


async def _drain_redis_stream(
    redis_url: str, stream_name: str = "events.raw.monitoring"
) -> list[dict[str, Any]]:
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
                    event = CanonicalEvent.from_redis_dict(decoded)
                    events.append(event.model_dump(mode="json"))
        return events
    finally:
        await client.aclose()


async def _flush_redis_stream(redis_url: str, stream_name: str = "events.raw.monitoring") -> None:
    client = Redis.from_url(redis_url)
    try:
        await client.delete(stream_name)
    except Exception:
        pass
    finally:
        await client.aclose()


@pytest_asyncio.fixture(autouse=True)
async def github_installation() -> Any:
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

    yield {"tenant_id": tenant_id}

    await _flush_redis_stream(settings.redis_url)
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.bypass_rls = 'true'"))
        await conn.execute(
            text("DELETE FROM github_installations WHERE repo_identifier = :repo"),
            {"repo": REPO_NAME},
        )
        await conn.execute(
            text("DELETE FROM chat_users WHERE tenant_id = :tid"), {"tid": tenant_id}
        )
        await conn.execute(
            text("DELETE FROM enterprise_users WHERE tenant_id = :tid"), {"tid": tenant_id}
        )
    await engine.dispose()


async def test_cicd_webhook_ping() -> None:
    payload = {
        "zen": "Keep it logically awesome.",
        "hook_id": 123456,
        "repository": {"full_name": REPO_NAME, "private": True},
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes, "ping")

    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/cicd/github", content=payload_bytes, headers=hdrs)

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_cicd_webhook_workflow_run_failure() -> None:
    settings = get_settings()
    payload = {
        "action": "completed",
        "workflow_run": {
            "id": 9999,
            "name": "Integration Tests CI",
            "head_branch": "feat/cicd",
            "head_sha": "123456abcdef",
            "status": "completed",
            "conclusion": "failure",
            "html_url": "https://github.com/test_org/test_repo/actions/runs/9999",
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
        # No real GitHub API calls for failure content or PR linking.
        patch(
            "checkcord.adapters.cicd.providers.github.webhook._cicd_provider.fetch_failure_content",
            new_callable=AsyncMock,
            return_value=CIFailureContent(source="unavailable"),
        ),
        patch(
            "checkcord.adapters.code.providers.github.rest_client.GitHubRestClient.list_pulls_associated_with_commit",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/webhooks/cicd/github", content=payload_bytes, headers=hdrs
            )

    assert response.status_code == 202
    assert response.json()["status"] == "processed"

    events = await _drain_redis_stream(settings.redis_url, "events.raw.monitoring")
    assert len(events) == 1
    event = events[0]
    assert event["source_platform"] == "github"
    assert event["source_type"] == "ci_pipeline"
    assert event["event_action"] == "created"
    assert event["source_id"] == "github:workflow_run:9999"
    assert "Integration Tests CI" in event["content_raw"]
    assert "failure" in event["content_raw"]


async def test_cicd_webhook_workflow_run_success_ignored() -> None:
    settings = get_settings()
    payload = {
        "action": "completed",
        "workflow_run": {
            "id": 10000,
            "name": "Integration Tests CI",
            "head_branch": "feat/cicd",
            "head_sha": "123456abcdef",
            "status": "completed",
            "conclusion": "success",
            "html_url": "https://github.com/test_org/test_repo/actions/runs/10000",
            "created_at": "2026-06-24T12:00:00Z",
            "updated_at": "2026-06-24T12:05:00Z",
        },
        "repository": {"full_name": REPO_NAME, "private": True},
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes, "workflow_run")

    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/cicd/github", content=payload_bytes, headers=hdrs)

    assert response.status_code == 202
    assert response.json()["status"] == "ignored_run"

    events = await _drain_redis_stream(settings.redis_url, "events.raw.monitoring")
    assert len(events) == 0


# ---------------------------------------------------------------------------
# Failure-content fetch -> redaction -> extraction -> enriched CanonicalEvent
# -> file-level AST linking, through one webhook delivery.
# ---------------------------------------------------------------------------


async def test_cicd_webhook_workflow_run_failure_enriched_content_is_redacted_and_linked(
    github_installation: dict[str, Any],
) -> None:
    """A secret in raw CI failure content must never reach the persisted event,
    while the redacted failure content and the extracted summary do, and
    file-level AST linking is called with the right arguments.
    """
    settings = get_settings()
    payload = {
        "action": "completed",
        "workflow_run": {
            "id": 12345,
            "name": "Integration Tests CI",
            "head_branch": "main",
            "head_sha": "deadbeef123456",
            "status": "completed",
            "conclusion": "failure",
            "html_url": "https://github.com/test_org/test_repo/actions/runs/12345",
            "created_at": "2026-08-01T12:00:00Z",
            "updated_at": "2026-08-01T12:05:00Z",
            "path": ".github/workflows/ci.yml",
        },
        "repository": {"full_name": REPO_NAME, "private": True},
    }
    payload_bytes = json.dumps(payload).encode()
    hdrs = _headers(payload_bytes, "workflow_run")

    leaked_secret = "ghp_" + "a" * 36  # GitHub-PAT-shaped, matches _TOKEN_PATTERNS
    raw_log = (
        f"tests/test_payment_webhook.py:44: AssertionError: expected 200, got 500\n"
        f"Using deploy token {leaked_secret} for retry\n"
    )

    from checkcord.core.redaction import redact_regex
    from checkcord.models.cicd import CIFailureContent, ExtractedCIFailure

    async def _fast_regex_only_redact(text: str, llm_client: Any = None) -> str:
        # No live LLM call: this exercises the regex fallback that
        # redact_text uses when the LLM is unavailable.
        return redact_regex(text or "")

    with (
        patch(
            "checkcord.adapters.code.providers.github.parser.get_repository_acl",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "checkcord.adapters.cicd.providers.github.webhook._cicd_provider.fetch_failure_content",
            new_callable=AsyncMock,
            return_value=CIFailureContent(source="job_log", log_excerpt=raw_log),
        ),
        patch(
            "checkcord.adapters.cicd.providers.github.webhook._cicd_provider.fetch_workflow_definition",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "checkcord.adapters.cicd.providers.github.webhook.redact_text",
            new_callable=AsyncMock,
            side_effect=_fast_regex_only_redact,
        ),
        patch(
            "checkcord.adapters.cicd.providers.github.webhook.extract_ci_failure",
            new_callable=AsyncMock,
            return_value=[
                ExtractedCIFailure(
                    failing_test_name="test_payment_webhook",
                    file_path="src/payments.py",
                    line_number=44,
                    error_message="AssertionError: expected 200, got 500",
                    summary="test_payment_webhook failed: expected HTTP 200 but got 500",
                )
            ],
        ),
        patch(
            "checkcord.adapters.cicd.providers.github.webhook.link_ci_failure_to_contexts",
            new_callable=AsyncMock,
            return_value=1,
        ) as mock_link,
    ):
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/webhooks/cicd/github", content=payload_bytes, headers=hdrs
            )

    assert response.status_code == 202, response.text
    event_id = response.json()["event_id"]

    events = await _drain_redis_stream(settings.redis_url, "events.raw.monitoring")
    assert len(events) == 1
    event = events[0]

    # 1. The leaked secret must NEVER appear in the persisted event.
    assert leaked_secret not in event["content_raw"]
    assert leaked_secret not in event["content_clean"]
    assert "[REDACTED:TOKEN]" in event["content_raw"]

    # 2. Real failure content (redacted) is present, not only the summary.
    assert "test_payment_webhook.py:44" in event["content_raw"]
    assert "AssertionError" in event["content_raw"]

    # 3. Structured extraction summary is attached as metadata.
    assert "extracted_failure_summaries" in event["metadata"]
    assert any(
        "test_payment_webhook" in s for s in event["metadata"]["extracted_failure_summaries"]
    )

    # 4. File-level AST linking was invoked with the right correlation data.
    mock_link.assert_called_once()
    link_kwargs = mock_link.call_args.kwargs
    assert str(link_kwargs["ci_event_id"]) == event_id
    assert link_kwargs["repo_identifier"] == REPO_NAME
    assert link_kwargs["commit_sha"] == "deadbeef123456"
    assert link_kwargs["extracted_file_path"] == "src/payments.py"


def _deliver_failed_run(url: str, link: AsyncMock) -> dict[str, Any]:
    from checkcord.core.redaction import redact_regex
    from checkcord.models.cicd import CIFailureContent, ExtractedCIFailure

    async def _regex_redact(text: str, llm_client: Any = None) -> str:
        return redact_regex(text)

    payload = {
        "action": "completed",
        "workflow_run": {
            "id": 4242,
            "name": "CI",
            "head_branch": "main",
            "head_sha": "cafe4242",
            "status": "completed",
            "conclusion": "failure",
            "html_url": "https://github.com/test_org/test_repo/actions/runs/4242",
            "created_at": "2026-08-01T12:00:00Z",
            "updated_at": "2026-08-01T12:05:00Z",
            "path": ".github/workflows/ci.yml",
        },
        "repository": {"full_name": REPO_NAME, "private": True},
    }
    body = json.dumps(payload).encode()
    module = "checkcord.adapters.cicd.providers.github.webhook"
    definition = AsyncMock(return_value=None)
    with (
        patch(
            "checkcord.adapters.code.providers.github.parser.get_repository_acl",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            f"{module}._cicd_provider.fetch_failure_content",
            new_callable=AsyncMock,
            return_value=CIFailureContent(source="job_log", log_excerpt="src/app.py:7: boom\n"),
        ),
        patch(f"{module}._cicd_provider.fetch_workflow_definition", definition),
        patch(f"{module}.redact_text", new_callable=AsyncMock, side_effect=_regex_redact),
        patch(
            f"{module}.extract_ci_failure",
            new_callable=AsyncMock,
            return_value=[
                ExtractedCIFailure(
                    file_path="src/app.py", error_message="boom", summary="app failed"
                )
            ],
        ),
        patch(f"{module}.link_ci_failure_to_contexts", link),
        TestClient(app) as client,
    ):
        response = client.post(url, content=body, headers=_headers(body, "workflow_run"))
    assert response.status_code == 202, response.text
    definition.assert_awaited_once()
    return dict(response.json())


async def test_main_webhook_handles_workflow_run_like_cicd_webhook() -> None:
    redis_url = get_settings().redis_url
    published: dict[str, list[dict[str, Any]]] = {}
    for url in ("/api/v1/webhooks/cicd/github", "/api/v1/webhooks/github"):
        link = AsyncMock(return_value=1)
        result = _deliver_failed_run(url, link)
        assert result["status"] == "processed"
        link.assert_awaited_once()
        assert link.call_args.kwargs["extracted_file_path"] == "src/app.py"
        events = await _drain_redis_stream(redis_url, "events.raw.monitoring")
        code_events = await _drain_redis_stream(redis_url, "events.raw.code")
        assert code_events == []
        assert [e["event_id"] for e in events] == [result["event_id"]]
        published[url] = [
            {k: v for k, v in e.items() if k not in ("event_id", "ingested_at")} for e in events
        ]
        await _flush_redis_stream(redis_url)

    assert published["/api/v1/webhooks/github"] == published["/api/v1/webhooks/cicd/github"]
    event = published["/api/v1/webhooks/github"][0]
    assert "src/app.py:7: boom" in event["content_raw"]
    assert event["metadata"]["extracted_failure_summaries"] == ["app failed"]

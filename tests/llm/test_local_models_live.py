"""Smoke test against a real OpenAI-compatible model server, such as Ollama.

LIVE_LLM_BASE_URL=http://localhost:11434/v1 LIVE_LLM_MODEL=qwen2.5-coder:7b \\
LIVE_EMBEDDING_MODEL=mxbai-embed-large LIVE_EMBEDDING_DIMENSION=1024 \\
uv run pytest tests/llm/test_local_models_live.py
"""

import os

import pytest

from checkcord.core.config import Settings
from checkcord.llm.client import LLMClient
from checkcord.llm.factory import build_chat_model
from checkcord.pipeline import redaction
from checkcord.pipeline.embedder import EmbeddingClient
from checkcord.pipeline.embeddings_factory import build_embeddings_provider

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.live(
        "LIVE_LLM_BASE_URL", "LIVE_LLM_MODEL", "LIVE_EMBEDDING_MODEL", "LIVE_EMBEDDING_DIMENSION"
    ),
]


def _settings() -> Settings:
    return Settings(
        environment="test",
        database_url="postgresql+asyncpg://u:p@localhost:5432/checkcord",
        openai_api_key=os.environ.get("LIVE_LLM_API_KEY", "local"),
        llm_base_url=os.environ["LIVE_LLM_BASE_URL"],
        llm_timeout_seconds=float(os.environ.get("LIVE_LLM_TIMEOUT_SECONDS", "120")),
        embedding_provider="openai",
        embedding_model=os.environ["LIVE_EMBEDDING_MODEL"],
        embedding_base_url=os.environ["LIVE_LLM_BASE_URL"],
        embedding_dimension=int(os.environ["LIVE_EMBEDDING_DIMENSION"]),
    )


async def test_linkage_verdicts() -> None:
    settings = _settings()
    client = LLMClient(model=build_chat_model(os.environ["LIVE_LLM_MODEL"], settings=settings))
    summary = "Decision: the billing service stores money as integer cents."
    same = "Billing amounts are integer cents, not floats."
    assert await client.judge_linkage(summary, same) == "same"
    other = "The office moves to the third floor."
    assert await client.judge_linkage(summary, other) == "different"


async def test_redaction_finds_an_email() -> None:
    settings = _settings()
    model = build_chat_model(os.environ["LIVE_LLM_MODEL"], settings=settings)
    text = "Questions go to rates-team@example.com."
    assert "rates-team@example.com" not in await redaction._model_pass(text, model)


async def test_embedding_width() -> None:
    settings = _settings()
    provider = build_embeddings_provider(settings)
    assert provider is not None
    client = EmbeddingClient(provider, dimension=settings.embedding_dimension)
    vector = await client.embed_text("checkcord")
    assert vector is not None and len(vector) == settings.embedding_dimension


ARGUMENT = [
    ("Tomas", "We should move everything into a monorepo."),
    ("Dev", "Strong no. Our CI would take forever."),
    ("Omar", "Monorepo means one blast radius for every bad merge."),
    ("Aiko", "But cross-repo changes are painful right now."),
    ("Rosa", "Access control gets harder in one repo."),
    ("Tomas", "CODEOWNERS handles that."),
    ("Dev", "CODEOWNERS is not access control."),
    ("Ingrid", "What problem are we solving exactly?"),
    ("Aiko", "Version drift between web-app and payments-api."),
    ("Omar", "Pin versions then."),
    ("Tomas", "Pinning is how we got the drift."),
    ("Dev", "Bazel is a full-time job."),
    ("Tomas", "Nobody said Bazel."),
    ("Rosa", "Then what tooling?"),
    ("Aiko", "Turborepo maybe, or just workspaces."),
    ("Omar", "Workspaces for Python are not great."),
    ("Dev", "I still think this is a solution looking for a problem."),
    ("Tomas", "It is a real problem, we lose a day a month on it."),
    ("Ingrid", "Can someone measure that?"),
    ("Aiko", "I can pull the numbers from the last quarter."),
    ("Omar", "Also the deploy pipeline assumes one repo per service."),
    ("Dev", "Exactly, that is weeks of work."),
    ("Tomas", "Weeks once versus a day every month."),
    ("Rosa", "I am not convinced either way."),
    ("Dev", "Let's argue about it again next quarter."),
]


def _discussion(client: LLMClient, lines: list[tuple[str, str]]) -> str:
    return client.discussion_text(
        [{"author_name": who, "content": text, "happened_at": None} for who, text in lines]
    )


@pytest.mark.parametrize(
    ("lines", "statement", "expected"),
    [
        (ARGUMENT, "Move everything into a monorepo using Turborepo.", "OPEN"),
        (
            [("Aiko", "Refunds over 500 EUR need a second approver.")],
            "Refunds over 500 EUR need a second approver.",
            "SETTLED",
        ),
        (
            [
                ("Tomas", "Proposal: we retry failed webhooks with exponential backoff."),
                ("Aiko", "Agreed, let's do it."),
            ],
            "Failed webhooks are retried with exponential backoff.",
            "SETTLED",
        ),
        (
            [
                ("Tomas", "Proposal: we retry failed webhooks with exponential backoff."),
                ("Dev", "I object, retries will double-charge customers until we have keys."),
            ],
            "Failed webhooks are retried with exponential backoff.",
            "OPEN",
        ),
    ],
)
async def test_settled_verdicts(
    lines: list[tuple[str, str]], statement: str, expected: str
) -> None:
    settings = _settings()
    client = LLMClient(model=build_chat_model(os.environ["LIVE_LLM_MODEL"], settings=settings))
    assert await client.judge_settled(_discussion(client, lines), statement) == expected

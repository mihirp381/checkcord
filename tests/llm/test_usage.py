from typing import Any
from uuid import uuid4

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatResult

from checkcord.llm import usage
from checkcord.pipeline.embedder import EmbeddingClient
from checkcord.workers.judge_batch_worker import _record_batch_usage

pytestmark = pytest.mark.asyncio

SECRET_PROMPT = "prompt text with carol@example.com that must not be counted"


@pytest.fixture(autouse=True)
def _empty() -> Any:
    usage.drain()
    yield
    usage.drain()


def _fields(pending: dict[usage.Key, usage.Counts]) -> list[Any]:
    return [part for key, counts in pending.items() for part in (*key, *vars(counts).values())]


async def test_chat_call_counts_tokens_and_keeps_no_content() -> None:
    tenant = uuid4()
    reply = AIMessage(
        content="reply text bob@example.com",
        usage_metadata={"input_tokens": 12, "output_tokens": 5, "total_tokens": 17},
    )
    model = FakeMessagesListChatModel(
        responses=[reply, reply], callbacks=[usage.UsageCallback("meeting_adr", "m-1")]
    )
    with usage.for_tenant(tenant):
        await model.ainvoke(SECRET_PROMPT)
        await model.ainvoke(SECRET_PROMPT)

    pending = usage.drain()
    ((key, counts),) = pending.items()
    assert key[0] == tenant
    assert key[2:] == (usage.CHAT, "meeting_adr", "m-1")
    assert (counts.calls, counts.failures) == (2, 0)
    assert (counts.input_tokens, counts.output_tokens) == (24, 10)
    for part in _fields(pending):
        assert "example.com" not in str(part)


class _Down(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "down"

    def _generate(self, *args: Any, **kwargs: Any) -> ChatResult:
        raise RuntimeError("provider down")


async def test_failed_chat_call_counts_a_failure() -> None:
    model = _Down(callbacks=[usage.UsageCallback("redaction", "m-2")])
    with pytest.raises(RuntimeError):
        await model.ainvoke("x")
    ((key, counts),) = usage.drain().items()
    assert key[2:] == (usage.CHAT, "redaction", "m-2")
    assert (counts.calls, counts.failures) == (1, 1)


class _Embeddings:
    model = "embed-1"

    async def aembed_query(self, text: str) -> list[float]:
        return [0.1, 0.2]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2] for _ in texts]


async def test_embedding_calls_are_counted_per_tenant_without_content() -> None:
    tenant = uuid4()
    client = EmbeddingClient(_Embeddings(), purpose="search")
    usage.set_tenant(str(tenant))
    await client.embed_text(SECRET_PROMPT)
    await client.embed_texts([SECRET_PROMPT, "second"])
    usage.set_tenant(None)

    pending = usage.drain()
    ((key, counts),) = pending.items()
    assert key[0] == tenant
    assert key[2:] == (usage.EMBEDDING, "search", "embed-1")
    assert (counts.calls, counts.failures) == (2, 0)
    for part in _fields(pending):
        assert "example.com" not in str(part)


async def test_batch_judge_output_lines_are_counted() -> None:
    tenant = uuid4()
    ok = {
        "response": {
            "body": {"model": "m-3", "usage": {"prompt_tokens": 7, "completion_tokens": 2}}
        }
    }
    _record_batch_usage(tenant, "m-default", ok)
    _record_batch_usage(tenant, "m-default", {"error": {"message": "bad"}})
    pending = usage.drain()
    assert pending[(tenant, next(iter(pending))[1], usage.CHAT, "meeting_judge_batch", "m-3")] == (
        usage.Counts(calls=1, input_tokens=7, output_tokens=2)
    )
    failed = [c for k, c in pending.items() if k[4] == "m-default"]
    assert failed == [usage.Counts(calls=1, failures=1)]


async def test_unknown_tenant_string_is_not_attributed() -> None:
    usage.set_tenant("not-a-uuid")
    usage.record(usage.RERANK, "rerank", "r-1")
    ((key, _),) = usage.drain().items()
    assert key[0] is None

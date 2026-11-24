"""Replies from small and reasoning models: tags, prose and unclear verdicts."""

from typing import Any

import pytest
from langchain_core.messages import AIMessage

from checkcord.llm.client import LLMClient
from checkcord.llm.parsing import (
    extract_json_object,
    parse_label,
    parse_verdict,
    strip_reasoning,
)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("<think>maybe FALSE?</think>\nTRUE", "TRUE"),
        ("<Thinking>long</Thinking> answer", "answer"),
        ("<reasoning>a</reasoning>x<think>b</think>y", "xy"),
        ("<think>still going, TRUE", ""),
    ],
)
def test_strip_reasoning(reply: str, expected: str) -> None:
    assert strip_reasoning(reply) == expected


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("TRUE", True),
        ("false.", False),
        ("**True** - same topic", True),
        ("<think>TRUE or FALSE</think> FALSE", False),
        ("UNTRUE", None),
        ("TRUE or FALSE", None),
        ("", None),
    ],
)
def test_parse_verdict(reply: str, expected: bool | None) -> None:
    assert parse_verdict(reply, "TRUE", "FALSE") is expected


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("SAME", "SAME"),
        ("changes, because it reverses it", "CHANGES"),
        ("<think>SAME? CHANGES?</think> Different", "DIFFERENT"),
        ("SAMENESS", None),
        ("SAME, not DIFFERENT", None),
        ("SAME same", "SAME"),
        ("", None),
    ],
)
def test_parse_label(reply: str, expected: str | None) -> None:
    assert parse_label(reply, ("SAME", "CHANGES", "DIFFERENT")) == expected


@pytest.mark.parametrize(
    "reply",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        'Here you go: {"a": 1} hope that helps {"b": 2}',
        '<think>{"a": 0</think>Sure. {not json} {"a": 1}',
    ],
)
def test_extract_json_object(reply: str) -> None:
    assert extract_json_object(reply) == {"a": 1}


def test_extract_json_object_without_one() -> None:
    with pytest.raises(ValueError, match="no JSON object"):
        extract_json_object("[1, 2] and prose")


class Replies:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def ainvoke(self, messages: Any) -> AIMessage:
        self.calls += 1
        return AIMessage(content=self.replies.pop(0))


def _client(*replies: str) -> tuple[LLMClient, Replies]:
    model = Replies(*replies)
    return LLMClient(model=model), model


@pytest.mark.asyncio
async def test_linkage_verdict_after_reasoning() -> None:
    client, _ = _client("<think>It might be DIFFERENT.</think>\nSAME")
    assert await client.judge_linkage("summary", "content") == "same"


@pytest.mark.asyncio
async def test_linkage_reads_each_label() -> None:
    for reply, verdict in (("**Changes**", "changes"), ("different.", "different")):
        client, _ = _client(reply)
        assert await client.judge_linkage("summary", "content") == verdict


@pytest.mark.asyncio
async def test_linkage_retries_an_unclear_reply_once() -> None:
    client, model = _client("I am not sure.", "CHANGES")
    assert await client.judge_linkage("summary", "content") == "changes"
    assert model.calls == 2


@pytest.mark.asyncio
async def test_linkage_raises_when_still_unclear() -> None:
    client, model = _client("Maybe.", "SAME or CHANGES")
    with pytest.raises(ValueError, match="not one of SAME, CHANGES, DIFFERENT"):
        await client.judge_linkage("summary", "content")
    assert model.calls == 2


@pytest.mark.asyncio
async def test_change_verdict_and_reason() -> None:
    client, _ = _client("<think>floats?</think>\nCONFLICTS: quote_total now returns a float.")
    assert await client.judge_change("Use integer cents.", "+ return 1.5") == (
        "conflicts",
        "quote_total now returns a float.",
    )


@pytest.mark.asyncio
async def test_change_reason_is_cut_and_redacted() -> None:
    key = "sk-" + "a1B2" * 12
    client, _ = _client(f"COMPLIES {'x' * 400}", f"Unrelated. It logs {key}")
    verdict, reason = await client.judge_change("d", "diff")
    assert (verdict, len(reason)) == ("complies", 300)
    verdict, reason = await client.judge_change("d", "diff")
    assert verdict == "unrelated"
    assert key not in reason


@pytest.mark.asyncio
async def test_change_verdict_retries_then_raises() -> None:
    client, model = _client("Hard to say.", "COMPLIES")
    assert (await client.judge_change("d", "diff"))[0] == "complies"
    assert model.calls == 2
    client, model = _client("Hard to say.", "Still unsure.")
    with pytest.raises(ValueError, match="CONFLICTS, COMPLIES, UNRELATED"):
        await client.judge_change("d", "diff")


@pytest.mark.asyncio
async def test_unclear_chat_classification_keeps_the_message() -> None:
    client, _ = _client("<think>hmm</think> hard to say")
    assert await client.classify_chat_message("lunch?") is True


@pytest.mark.asyncio
async def test_chat_classification_reads_noise() -> None:
    client, _ = _client("NOISE")
    assert await client.classify_chat_message("lunch?") is False


@pytest.mark.asyncio
async def test_discourse_router_retries_prose_then_reads_json() -> None:
    client, _ = _client(
        "Let me think about it.",
        'Sure! {"tree_id": "t1", "domain_tag": "solution_space", "summary": "cache"} done',
    )
    result = await client.assign_discourse_tree([], [{"text": "use a cache"}])
    assert result == {"tree_id": "t1", "domain_tag": "SOLUTION_SPACE", "summary": "cache"}

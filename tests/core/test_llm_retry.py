from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from checkcord.core.llm_retry import DEFAULT_MAX_RETRIES, ainvoke_with_retry


class _DummyOutput(BaseModel):
    value: str


@pytest.mark.asyncio
async def test_ainvoke_with_retry_succeeds_first_try() -> None:
    structured_llm = AsyncMock()
    structured_llm.ainvoke.return_value = _DummyOutput(value="ok")

    result = await ainvoke_with_retry(structured_llm, ["msg"])

    assert result == _DummyOutput(value="ok")
    assert structured_llm.ainvoke.call_count == 1


@pytest.mark.asyncio
async def test_ainvoke_with_retry_succeeds_after_one_validation_failure() -> None:
    """The LLM returns invalid output once (with_structured_output raises on a
    schema validation failure), then valid output on the retry.
    """
    structured_llm = AsyncMock()
    structured_llm.ainvoke.side_effect = [
        ValueError("schema validation failed: missing required field"),
        _DummyOutput(value="recovered"),
    ]

    result = await ainvoke_with_retry(structured_llm, ["msg"], max_retries=2)

    assert result == _DummyOutput(value="recovered")
    assert structured_llm.ainvoke.call_count == 2


@pytest.mark.asyncio
async def test_ainvoke_with_retry_retries_use_original_messages_unchanged() -> None:
    """A retry must not feed the earlier invalid output back into the prompt:
    every attempt gets the same original messages.
    """
    structured_llm = AsyncMock()
    structured_llm.ainvoke.side_effect = [
        ValueError("bad output"),
        _DummyOutput(value="ok"),
    ]
    original_messages = [("system", "prompt"), ("user", "content")]

    await ainvoke_with_retry(structured_llm, original_messages, max_retries=2)

    for call in structured_llm.ainvoke.call_args_list:
        assert call.args[0] == original_messages
        assert call.args[0] is original_messages


@pytest.mark.asyncio
async def test_ainvoke_with_retry_cap_is_enforced_then_raises() -> None:
    """A systematically wrong schema must not retry forever -- it exhausts the
    cap (default 2 retries / 3 attempts total) and raises the final error
    rather than looping, letting the caller's own except-and-fall-through path
    handle it exactly as an uncaught first-attempt failure would have."""
    structured_llm = AsyncMock()
    always_fails = RuntimeError("persistently invalid schema")
    structured_llm.ainvoke.side_effect = always_fails

    with pytest.raises(RuntimeError, match="persistently invalid schema"):
        await ainvoke_with_retry(structured_llm, ["msg"], max_retries=2)

    assert structured_llm.ainvoke.call_count == 1 + 2  # initial attempt + 2 retries


@pytest.mark.asyncio
async def test_ainvoke_with_retry_default_max_retries_is_two() -> None:
    assert DEFAULT_MAX_RETRIES == 2

    structured_llm = AsyncMock()
    structured_llm.ainvoke.side_effect = RuntimeError("always fails")

    with pytest.raises(RuntimeError):
        await ainvoke_with_retry(structured_llm, ["msg"])  # no explicit max_retries

    assert structured_llm.ainvoke.call_count == 1 + DEFAULT_MAX_RETRIES


@pytest.mark.asyncio
async def test_ainvoke_with_retry_calls_before_attempt_once_per_attempt() -> None:
    """before_attempt (e.g. the caller's rate limiter) must run before every
    real LLM call, including retries -- each retry is a genuine new API call
    and must be accounted for like one, not just the first attempt."""
    structured_llm = AsyncMock()
    structured_llm.ainvoke.side_effect = [
        ValueError("bad output"),
        _DummyOutput(value="ok"),
    ]
    before_attempt_calls = 0

    async def before_attempt() -> None:
        nonlocal before_attempt_calls
        before_attempt_calls += 1

    await ainvoke_with_retry(structured_llm, ["msg"], max_retries=2, before_attempt=before_attempt)

    assert before_attempt_calls == 2  # one per actual attempt made (initial + 1 retry)

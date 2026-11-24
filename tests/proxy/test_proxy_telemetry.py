import json

import pytest

from checkcord.proxy.telemetry import (
    SSEParser,
    UsageAccumulator,
    calculate_cost_and_savings,
    extract_usage_from_body,
    get_model_pricing,
)


def test_sse_parser() -> None:
    parser = SSEParser()
    chunk1 = b'data: {"test": 1}\n\ndata: {"test":'
    chunk2 = b" 2}\n\n"

    events = parser.feed(chunk1)
    assert events == ['{"test": 1}']

    events = parser.feed(chunk2)
    assert events == ['{"test": 2}']


def test_usage_accumulator_openai() -> None:
    accumulator = UsageAccumulator(provider="openai")

    # Chunk with partial usage info
    event1 = json.dumps(
        {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 40},
            }
        }
    )

    accumulator.feed_event(event1)

    # Subsequent chunk with identical usage (cumulative)
    event2 = json.dumps(
        {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 15,
                "prompt_tokens_details": {"cached_tokens": 40},
            }
        }
    )

    accumulator.feed_event(event2)

    res = accumulator.get_dict()
    assert res["prompt_tokens"] == 100
    assert res["completion_tokens"] == 15
    assert res["cache_creation_input_tokens"] == 0
    assert res["cache_read_input_tokens"] == 40


def test_usage_accumulator_anthropic() -> None:
    accumulator = UsageAccumulator(provider="anthropic")

    # Message start chunk containing input tokens and caching details
    event1 = json.dumps(
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 200,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 50,
                }
            },
        }
    )
    accumulator.feed_event(event1)

    # Message delta chunk updating output tokens
    event2 = json.dumps({"type": "message_delta", "usage": {"output_tokens": 50}})
    accumulator.feed_event(event2)

    res = accumulator.get_dict()
    assert res["prompt_tokens"] == 200
    assert res["completion_tokens"] == 50
    assert res["cache_creation_input_tokens"] == 100
    assert res["cache_read_input_tokens"] == 50


def test_extract_usage_from_body() -> None:
    # OpenAI body
    openai_body = {
        "usage": {
            "prompt_tokens": 150,
            "completion_tokens": 25,
            "prompt_tokens_details": {"cached_tokens": 60},
        }
    }
    res_oa = extract_usage_from_body(openai_body, "openai")
    assert res_oa["prompt_tokens"] == 150
    assert res_oa["completion_tokens"] == 25
    assert res_oa["cache_read_input_tokens"] == 60
    assert res_oa["cache_creation_input_tokens"] == 0

    # Anthropic body
    anthropic_body = {
        "usage": {
            "input_tokens": 300,
            "output_tokens": 80,
            "cache_creation_input_tokens": 150,
            "cache_read_input_tokens": 100,
        }
    }
    res_ant = extract_usage_from_body(anthropic_body, "anthropic")
    assert res_ant["prompt_tokens"] == 300
    assert res_ant["completion_tokens"] == 80
    assert res_ant["cache_creation_input_tokens"] == 150
    assert res_ant["cache_read_input_tokens"] == 100


def test_cost_and_savings_calculation() -> None:
    # Claude 3.5 Sonnet standard pricing:
    # Input: $3/1M, Output: $15/1M, Cache Read: $0.30/1M, Cache Creation: $3.75/1M

    # Test savings calculation when caching is active
    # Input tokens = 1,000,000. Cache read = 500,000. Cache creation = 200,000. Output = 100,000.
    # Cost with caching:
    #   Non-cached input = 1M - 500k - 200k = 300,000
    #   Non-cached input cost = 300k * $3/1M = $0.90
    #   Cache read cost = 500k * $0.30/1M = $0.15
    #   Cache creation cost = 200k * $3.75/1M = $0.75
    #   Output cost = 100k * $15/1M = $1.50
    #   Total Cost = $0.90 + $0.15 + $0.75 + $1.50 = $3.30

    # Cost without caching:
    #   Input = 1,000,000 * $3/1M = $3.00
    #   Output = 100,000 * $15/1M = $1.50
    #   Total Cost = $3.00 + $1.50 = $4.50

    # Savings = $4.50 - $3.30 = $1.20

    cost, savings = calculate_cost_and_savings(
        provider="anthropic",
        model="claude-3-5-sonnet",
        prompt_tokens=1_000_000,
        completion_tokens=100_000,
        cache_creation_input_tokens=200_000,
        cache_read_input_tokens=500_000,
    )

    assert pytest.approx(cost) == 3.30
    assert pytest.approx(savings) == 1.20


def test_pricing_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    # Set overrides
    monkeypatch.setenv("CHECKCORD_OPENAI_INPUT_PRICE", "10.00")
    monkeypatch.setenv("CHECKCORD_OPENAI_OUTPUT_PRICE", "30.00")
    monkeypatch.setenv("CHECKCORD_OPENAI_CACHE_READ_PRICE", "5.00")

    rates = get_model_pricing("openai", "gpt-4o")
    assert rates["input"] == 10.00
    assert rates["output"] == 30.00
    assert rates["cache_read"] == 5.00
    # cache_creation defaults to 5.00
    assert rates["cache_creation"] == 5.00

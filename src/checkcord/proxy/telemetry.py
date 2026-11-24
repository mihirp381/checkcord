import json
import os
from typing import Any


class SSEParser:
    """Buffers incoming chunks and yields complete server-sent event (SSE) data lines."""

    def __init__(self) -> None:
        self.buffer = ""

    def feed(self, chunk: bytes) -> list[str]:
        """Feed a chunk of bytes and return a list of JSON data payloads."""
        self.buffer += chunk.decode("utf-8", errors="ignore")
        lines = self.buffer.split("\n")
        self.buffer = lines.pop()  # Keep partial line in buffer

        events = []
        for line in lines:
            line = line.strip()
            if line.startswith("data:"):
                data_str = line[5:].strip()
                if data_str and data_str != "[DONE]":
                    events.append(data_str)
        return events


class UsageAccumulator:
    """Accumulates usage metrics over streaming chunks."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0

    def feed_event(self, event_data: str) -> None:
        """Parse a single SSE JSON payload and update accumulated usage stats."""
        try:
            data = json.loads(event_data)
            if self.provider == "openai":
                usage = data.get("usage")
                if usage:
                    self.prompt_tokens = max(self.prompt_tokens, usage.get("prompt_tokens", 0))
                    self.completion_tokens = max(
                        self.completion_tokens, usage.get("completion_tokens", 0)
                    )
                    cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                    self.cache_read_input_tokens = max(self.cache_read_input_tokens, cached)
            elif self.provider == "anthropic":
                event_type = data.get("type")
                if event_type == "message_start":
                    message = data.get("message", {})
                    usage = message.get("usage", {})
                    self.prompt_tokens = max(self.prompt_tokens, usage.get("input_tokens", 0))
                    self.completion_tokens = max(
                        self.completion_tokens, usage.get("output_tokens", 0)
                    )
                    self.cache_creation_input_tokens = max(
                        self.cache_creation_input_tokens,
                        usage.get("cache_creation_input_tokens", 0),
                    )
                    self.cache_read_input_tokens = max(
                        self.cache_read_input_tokens, usage.get("cache_read_input_tokens", 0)
                    )
                elif event_type == "message_delta":
                    usage = data.get("usage", {})
                    self.completion_tokens = max(
                        self.completion_tokens, usage.get("output_tokens", 0)
                    )
        except Exception:
            pass

    def get_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
        }


def extract_usage_from_body(body: dict[str, Any], provider: str) -> dict[str, int]:
    """Extract token usage details from a non-streaming JSON response body."""
    res = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    try:
        if provider == "openai":
            usage = body.get("usage", {})
            res["prompt_tokens"] = usage.get("prompt_tokens", 0)
            res["completion_tokens"] = usage.get("completion_tokens", 0)
            res["cache_read_input_tokens"] = usage.get("prompt_tokens_details", {}).get(
                "cached_tokens", 0
            )
        elif provider == "anthropic":
            usage = body.get("usage", {})
            res["prompt_tokens"] = usage.get("input_tokens", 0)
            res["completion_tokens"] = usage.get("output_tokens", 0)
            res["cache_creation_input_tokens"] = usage.get("cache_creation_input_tokens", 0)
            res["cache_read_input_tokens"] = usage.get("cache_read_input_tokens", 0)
    except Exception:
        pass
    return res


# Hardcoded default token pricing per 1M tokens (in USD)
# Custom overrides can be supplied via ProxySettings or Environment Variables
DEFAULT_PRICING = {
    "openai": {
        "gpt-4o": {
            "input": 5.00,
            "output": 15.00,
            "cache_read": 2.50,
            "cache_creation": 5.00,
        },
        "gpt-4o-mini": {
            "input": 0.15,
            "output": 0.60,
            "cache_read": 0.075,
            "cache_creation": 0.15,
        },
    },
    "anthropic": {
        "claude-3-5-sonnet": {
            "input": 3.00,
            "output": 15.00,
            "cache_read": 0.30,
            "cache_creation": 3.75,
        },
        "claude-3-opus": {
            "input": 15.00,
            "output": 75.00,
            "cache_read": 1.50,
            "cache_creation": 18.75,
        },
        "claude-3-haiku": {
            "input": 0.25,
            "output": 1.25,
            "cache_read": 0.03,
            "cache_creation": 0.30,
        },
    },
}

# Fallback pricing (used if the exact model isn't matched)
FALLBACK_PRICING = {
    "input": 3.00,
    "output": 15.00,
    "cache_read": 0.30,
    "cache_creation": 3.75,
}


def get_model_pricing(provider: str, model: str) -> dict[str, float]:
    """Match the model name to get pricing rates per 1M tokens."""
    # Check for environment variable overrides first
    env_prefix = f"CHECKCORD_{provider.upper()}"
    env_input = os.environ.get(f"{env_prefix}_INPUT_PRICE")
    env_output = os.environ.get(f"{env_prefix}_OUTPUT_PRICE")
    env_cache_read = os.environ.get(f"{env_prefix}_CACHE_READ_PRICE")
    env_cache_creation = os.environ.get(f"{env_prefix}_CACHE_CREATION_PRICE")

    # Start with default pricing
    rates: dict[str, float] = {}
    provider_rates = DEFAULT_PRICING.get(provider, {})

    # Try exact match
    if model in provider_rates:
        rates = dict(provider_rates[model])
    else:
        # Try prefix match (e.g., gpt-4o-2024-08-06 matches gpt-4o)
        matched = False
        for model_key, r in provider_rates.items():
            if model.startswith(model_key):
                rates = dict(r)
                matched = True
                break
        if not matched:
            rates = dict(FALLBACK_PRICING)

    # Apply environment variable overrides if present
    if env_input is not None:
        try:
            rates["input"] = float(env_input)
        except ValueError:
            pass
    if env_output is not None:
        try:
            rates["output"] = float(env_output)
        except ValueError:
            pass
    if env_cache_read is not None:
        try:
            rates["cache_read"] = float(env_cache_read)
        except ValueError:
            pass
    if env_cache_creation is not None:
        try:
            rates["cache_creation"] = float(env_cache_creation)
        except ValueError:
            pass

    return rates


def calculate_cost_and_savings(
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
) -> tuple[float, float]:
    """Calculate the actual cost and the estimated cost saved (in USD)."""
    rates = get_model_pricing(provider, model)
    input_rate = rates["input"] / 1_000_000.0
    output_rate = rates["output"] / 1_000_000.0
    cache_read_rate = rates["cache_read"] / 1_000_000.0
    cache_creation_rate = rates["cache_creation"] / 1_000_000.0

    # Calculate actual cost:
    # prompt_tokens is the total input tokens (which includes cache hits & cache writes)
    non_cached_input_tokens = prompt_tokens - cache_read_input_tokens - cache_creation_input_tokens

    # Ensure no negative tokens
    non_cached_input_tokens = max(0, non_cached_input_tokens)

    actual_cost = (
        (non_cached_input_tokens * input_rate)
        + (cache_read_input_tokens * cache_read_rate)
        + (cache_creation_input_tokens * cache_creation_rate)
        + (completion_tokens * output_rate)
    )

    # Cost if caching wasn't used:
    # All cache_read_input_tokens and cache_creation_input_tokens would have been
    # billed at the standard input rate.
    # Note: Anthropic charges cache creation at a surcharge (1.25x), so we save
    # that surcharge as well.
    no_caching_cost = (prompt_tokens * input_rate) + (completion_tokens * output_rate)

    savings = max(0.0, no_caching_cost - actual_cost)
    return actual_cost, savings

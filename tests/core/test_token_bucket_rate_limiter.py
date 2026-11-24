"""Unit tests for AsyncTokenBucketRateLimiter."""

import time

import pytest

from checkcord.core.rate_limiter import AsyncTokenBucketRateLimiter, global_rate_limiter

pytestmark = pytest.mark.asyncio


async def test_rate_limiter_acquires_tokens() -> None:
    limiter = AsyncTokenBucketRateLimiter(max_tpm=60000, max_rpm=600, safety_factor=1.0)

    start_time = time.monotonic()
    # Acquire 10 requests fast
    for _ in range(10):
        await limiter.acquire(estimated_tokens=500)
    elapsed = time.monotonic() - start_time

    # Should take < 0.5s because capacity exists
    assert elapsed < 0.5


async def test_rate_limiter_throttles_when_exhausted() -> None:
    # 6000-token bucket refills 100 tokens/s, so 6 missing tokens cost ~0.1s
    limiter = AsyncTokenBucketRateLimiter(max_tpm=6000, max_rpm=6000, safety_factor=1.0)

    # Drain all initial tokens
    await limiter.acquire(estimated_tokens=6000)

    start_time = time.monotonic()
    # Next call requires tokens that aren't available yet, forcing wait
    await limiter.acquire(estimated_tokens=6)
    elapsed = time.monotonic() - start_time

    assert elapsed >= 0.05


async def test_global_rate_limiter_depletion_step_one() -> None:
    """Part 1 of 2: deplete the shared `global_rate_limiter` singleton.

    Without the autouse `reset_global_rate_limiter` fixture in tests/conftest.py
    this depletion would carry into whichever test runs next.
    """
    assert global_rate_limiter._tpm_tokens == global_rate_limiter.max_tpm
    await global_rate_limiter.acquire(estimated_tokens=global_rate_limiter.max_tpm)
    assert global_rate_limiter._tpm_tokens < 1


async def test_global_rate_limiter_is_reset_step_two() -> None:
    """Part 2 of 2: proves the autouse fixture reset the depletion from the test above.

    Relies on pytest running test files/functions in definition order within a module
    (the default, no randomization plugin active in this suite) so this genuinely
    exercises the fixture rather than coincidentally passing.
    """
    assert global_rate_limiter._tpm_tokens == global_rate_limiter.max_tpm
    assert global_rate_limiter._rpm_tokens == global_rate_limiter.max_rpm

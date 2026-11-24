"""Token bucket for model API calls."""

import asyncio
import time

import structlog

logger = structlog.get_logger(__name__)


class AsyncTokenBucketRateLimiter:
    """Token bucket limiting model API calls."""

    def __init__(
        self,
        max_tpm: int = 30000,
        max_rpm: int = 500,
        safety_factor: float = 0.85,
    ) -> None:
        self.max_tpm = int(max_tpm * safety_factor)
        self.max_rpm = int(max_rpm * safety_factor)

        self._tpm_tokens = float(self.max_tpm)
        self._rpm_tokens = float(self.max_rpm)

        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        delta = now - self._last_refill
        self._last_refill = now

        # Refill tokens proportional to elapsed time (per minute = 60s)
        self._tpm_tokens = min(self.max_tpm, self._tpm_tokens + (self.max_tpm / 60.0) * delta)
        self._rpm_tokens = min(self.max_rpm, self._rpm_tokens + (self.max_rpm / 60.0) * delta)

    async def acquire(self, estimated_tokens: int = 1000) -> None:
        """Acquire permission to execute an LLM call with estimated token consumption."""
        while True:
            async with self._lock:
                self._refill()

                if self._tpm_tokens >= estimated_tokens and self._rpm_tokens >= 1:
                    self._tpm_tokens -= estimated_tokens
                    self._rpm_tokens -= 1
                    return

                # Calculate sleep duration needed for tokens to replenish
                needed_tpm = max(0.0, estimated_tokens - self._tpm_tokens)
                needed_rpm = max(0.0, 1.0 - self._rpm_tokens)

                tpm_wait = (needed_tpm / (self.max_tpm / 60.0)) if needed_tpm > 0 else 0
                rpm_wait = (needed_rpm / (self.max_rpm / 60.0)) if needed_rpm > 0 else 0

                wait_time = max(tpm_wait, rpm_wait, 0.1)
                logger.debug(
                    "rate_limiter.throttling",
                    wait_seconds=wait_time,
                    estimated_tokens=estimated_tokens,
                )

            await asyncio.sleep(wait_time)


# Global default rate limiter instance
global_rate_limiter = AsyncTokenBucketRateLimiter()

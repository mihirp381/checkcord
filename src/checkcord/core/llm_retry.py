"""Retry-on-validation-failure wrapper for LangChain's ``with_structured_output()``.

``with_structured_output()`` on a bare chat model does not retry when the
response fails schema validation.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_MAX_RETRIES = 2


async def ainvoke_with_retry(
    structured_llm: Any,
    messages: list[Any],
    max_retries: int = DEFAULT_MAX_RETRIES,
    agent: str = "unknown",
    before_attempt: Callable[[], Awaitable[None]] | None = None,
) -> Any:
    """Call ``structured_llm.ainvoke(messages)``, retrying up to ``max_retries`` times.

    Each attempt re-sends the original ``messages``, never the model's invalid
    output, so a retry cannot anchor on it. The final exception propagates
    unchanged. ``before_attempt``, if given, is awaited before every attempt
    including the first (for example a rate limiter).
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        if before_attempt is not None:
            await before_attempt()
        try:
            return await structured_llm.ainvoke(messages)
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                logger.warning(
                    "llm_retry.structured_output_validation_failed",
                    agent=agent,
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    error=str(e),
                )
            else:
                logger.warning(
                    "llm_retry.structured_output_retries_exhausted",
                    agent=agent,
                    attempts=max_retries + 1,
                    error=str(e),
                )

    assert last_exc is not None  # the loop runs at least once (max_retries + 1 >= 1)
    raise last_exc

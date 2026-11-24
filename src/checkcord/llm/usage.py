"""Model usage counters per tenant, day, kind, purpose and model.

Counted in memory where the clients are built (chat models, the embedder, the
reranker) and flushed to ``model_usage``. No content and no per-call rows.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import astuple, dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import structlog
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

from checkcord.core import metrics

logger = structlog.get_logger(__name__)

CHAT, EMBEDDING, RERANK = "chat", "embedding", "rerank"

_tenant: ContextVar[UUID | None] = ContextVar("checkcord_usage_tenant", default=None)


def set_tenant(tenant_id: Any) -> None:
    try:
        _tenant.set(
            tenant_id if tenant_id is None or isinstance(tenant_id, UUID) else UUID(str(tenant_id))
        )
    except ValueError:
        _tenant.set(None)


@contextmanager
def for_tenant(tenant_id: UUID | None) -> Iterator[None]:
    token = _tenant.set(tenant_id)
    try:
        yield
    finally:
        _tenant.reset(token)


@dataclass
class Counts:
    calls: int = 0
    failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0

    def add(self, other: Counts) -> None:
        for name, value in zip(self.__dataclass_fields__, astuple(other), strict=True):
            setattr(self, name, getattr(self, name) + value)


Key = tuple[UUID | None, date, str, str, str]

_lock = threading.Lock()
_pending: dict[Key, Counts] = {}


def record(
    kind: str,
    purpose: str,
    model: str,
    *,
    latency_s: float = 0.0,
    failed: bool = False,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    tenant_id: UUID | None = None,
) -> None:
    metrics.MODEL_CALLS.labels(kind, purpose, model, "failed" if failed else "ok").inc()
    metrics.MODEL_TOKENS.labels(kind, purpose, model, "input").inc(input_tokens or 0)
    metrics.MODEL_TOKENS.labels(kind, purpose, model, "output").inc(output_tokens or 0)
    key = (tenant_id or _tenant.get(), datetime.now(UTC).date(), kind, purpose, model)
    counts = Counts(
        calls=1,
        failures=int(failed),
        input_tokens=input_tokens or 0,
        output_tokens=output_tokens or 0,
        latency_ms=round(latency_s * 1000),
    )
    with _lock:
        _pending.setdefault(key, Counts()).add(counts)


def drain() -> dict[Key, Counts]:
    global _pending
    with _lock:
        pending, _pending = _pending, {}
    return pending


def _restore(pending: dict[Key, Counts]) -> None:
    with _lock:
        for key, counts in pending.items():
            _pending.setdefault(key, Counts()).add(counts)


def _token_counts(response: LLMResult) -> tuple[int | None, int | None]:
    for generations in response.generations:
        for gen in generations:
            meta = getattr(getattr(gen, "message", None), "usage_metadata", None)
            if meta:
                return meta.get("input_tokens"), meta.get("output_tokens")
    usage = (response.llm_output or {}).get("token_usage") or {}
    return usage.get("prompt_tokens"), usage.get("completion_tokens")


class UsageCallback(AsyncCallbackHandler):
    """Counts each chat-model call of the model it is attached to."""

    def __init__(self, purpose: str, model: str) -> None:
        self.purpose = purpose
        self.model = model
        self._started: dict[UUID, tuple[float, UUID | None]] = {}

    async def on_chat_model_start(
        self, serialized: dict[str, Any], messages: Any, *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._started[run_id] = (time.perf_counter(), _tenant.get())

    async def on_llm_start(
        self, serialized: dict[str, Any], prompts: Any, *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._started[run_id] = (time.perf_counter(), _tenant.get())

    def _finish(self, run_id: UUID) -> tuple[float, UUID | None]:
        started, tenant_id = self._started.pop(run_id, (time.perf_counter(), _tenant.get()))
        return time.perf_counter() - started, tenant_id

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        latency, tenant_id = self._finish(run_id)
        input_tokens, output_tokens = _token_counts(response)
        record(
            CHAT,
            self.purpose,
            self.model,
            latency_s=latency,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tenant_id=tenant_id,
        )

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        latency, tenant_id = self._finish(run_id)
        record(CHAT, self.purpose, self.model, latency_s=latency, failed=True, tenant_id=tenant_id)


@contextmanager
def timed(kind: str, purpose: str, model: str) -> Iterator[dict[str, int | None]]:
    """Count one call made in the block; set ``input_tokens``/``output_tokens`` if known."""
    tokens: dict[str, int | None] = {"input_tokens": None, "output_tokens": None}
    started = time.perf_counter()
    try:
        yield tokens
    except Exception:
        record(kind, purpose, model, latency_s=time.perf_counter() - started, failed=True)
        raise
    record(kind, purpose, model, latency_s=time.perf_counter() - started, **tokens)


async def flush(engine: Any) -> int:
    """Write the counted usage to ``model_usage``; returns the rows written.

    Usage with no known tenant goes to ``DEFAULT_TENANT_ID`` or is dropped.
    Counts that cannot be written are kept for the next flush.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from checkcord.core.config import get_settings
    from checkcord.storage.models import ModelUsage
    from checkcord.storage.postgres import tenant_context

    pending = drain()
    if not pending:
        return 0
    default = get_settings().default_tenant_id
    by_tenant: dict[UUID, dict[Key, Counts]] = {}
    for (tenant_id, *rest), counts in pending.items():
        resolved = tenant_id or default
        if resolved is None:
            logger.warning("usage.dropped_no_tenant", calls=counts.calls)
            continue
        key: Key = (resolved, *rest)
        by_tenant.setdefault(resolved, {}).setdefault(key, Counts()).add(counts)

    table = ModelUsage.__table__
    written = 0
    for tenant_id, rows in by_tenant.items():
        values = [
            {
                "tenant_id": t,
                "day": day,
                "kind": kind,
                "purpose": purpose,
                "model": model,
                **c.__dict__,
            }
            for (t, day, kind, purpose, model), c in rows.items()
        ]
        stmt = pg_insert(table).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "day", "kind", "purpose", "model"],
            set_={
                name: table.c[name] + stmt.excluded[name] for name in Counts.__dataclass_fields__
            },
        )
        try:
            async with tenant_context(engine, tenant_id) as conn:
                await conn.execute(stmt)
            written += len(values)
        except Exception as e:
            logger.warning("usage.flush_failed", tenant_id=str(tenant_id), error=str(e))
            _restore(rows)
    return written

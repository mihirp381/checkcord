"""Model calls, tokens and estimated cost of one tenant over recent days."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.core.config import get_settings
from checkcord.storage import audit
from checkcord.storage.models import ModelUsage
from checkcord.storage.postgres import tenant_context

_u = ModelUsage.__table__.c


def cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Estimated cost from ``MODEL_PRICES``, or None when the model has no price."""
    price = get_settings().model_prices.get(model)
    if price is None:
        return None
    return round(input_tokens / 1e6 * price[0] + output_tokens / 1e6 * price[1], 6)


async def report(
    engine: AsyncEngine, tenant_id: UUID, days: int, actor: audit.Actor
) -> dict[str, Any]:
    since = datetime.now(UTC).date() - timedelta(days=days - 1)
    async with tenant_context(engine, tenant_id) as conn:
        rows = (
            await conn.execute(
                select(
                    _u.kind,
                    _u.purpose,
                    _u.model,
                    func.sum(_u.calls).label("calls"),
                    func.sum(_u.failures).label("failures"),
                    func.sum(_u.input_tokens).label("input_tokens"),
                    func.sum(_u.output_tokens).label("output_tokens"),
                    func.sum(_u.latency_ms).label("latency_ms"),
                )
                .where((_u.tenant_id == tenant_id) & (_u.day >= since))
                .group_by(_u.kind, _u.purpose, _u.model)
                .order_by(_u.kind, _u.purpose, _u.model)
            )
        ).all()
        await audit.record(
            conn, tenant_id, actor, "usage.read", params={"days": days}, result_count=len(rows)
        )
    priced = bool(get_settings().model_prices)
    out = []
    for r in rows:
        calls = int(r.calls)
        out.append(
            {
                "kind": r.kind,
                "purpose": r.purpose,
                "model": r.model,
                "calls": calls,
                "failures": int(r.failures),
                "input_tokens": int(r.input_tokens),
                "output_tokens": int(r.output_tokens),
                "avg_latency_ms": round(int(r.latency_ms) / calls) if calls else 0,
                "estimated_cost": cost(r.model, int(r.input_tokens), int(r.output_tokens))
                if priced
                else None,
            }
        )
    costs = [r["estimated_cost"] for r in out if r["estimated_cost"] is not None]
    return {
        "days": days,
        "since": since.isoformat(),
        "rows": out,
        "totals": {
            "calls": sum(r["calls"] for r in out),
            "failures": sum(r["failures"] for r in out),
            "input_tokens": sum(r["input_tokens"] for r in out),
            "output_tokens": sum(r["output_tokens"] for r in out),
            "estimated_cost": round(sum(costs), 6) if priced else None,
        },
    }

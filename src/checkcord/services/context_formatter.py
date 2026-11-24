import json
import os
from datetime import UTC, datetime
from typing import Any, NamedTuple
from uuid import UUID

import yaml
from jinja2 import Environment, FileSystemLoader
from sqlalchemy.ext.asyncio import AsyncConnection

from checkcord.core import redaction
from checkcord.core.agent_text import NOTICE
from checkcord.services import record_notes
from checkcord.services.repo_audience import RepoAudience
from checkcord.storage.postgres import BLOCKING, PostgresEventStore
from checkcord.workers.pr_status_reporter import _build_source_link

# context_type -> ARCHITECTURE.md section title, in rendered order.
_CONTEXT_TYPE_SECTION_TITLES: dict[str, str] = {
    "adr": "Decisions",
    "constraint": "Constraints",
    "risk": "Risks",
    "business_requirement": "Business Requirements",
    "general": "Blocking Records",
}
HEADING_CHARS = 120


def heading(summary: str) -> tuple[str, str | None]:
    """The first line cut to ``HEADING_CHARS``, and the lines below it; a cut first line
    is repeated in full in the body."""
    first, _, rest = summary.strip().partition("\n")
    first, rest = first.strip(), rest.strip()
    cut = first[:HEADING_CHARS]
    body = "\n".join(p for p in (first if cut != first else "", rest) if p)
    return cut, body or None


def front_matter(
    tenant_id: str, repo_identifier: str, compiled_at: str, contexts: list[dict[str, Any]]
) -> str:
    return str(
        yaml.safe_dump(
            {
                "tenant_id": tenant_id,
                "repo_identifier": repo_identifier,
                "compiled_at": compiled_at,
                "contexts": [_front_matter_entry(ctx) for ctx in contexts],
            },
            sort_keys=False,
            allow_unicode=True,
        )
    )


def _front_matter_entry(ctx: dict[str, Any]) -> dict[str, Any]:
    entry = {
        "id": ctx["id"],
        "summary": ctx["summary"],
        "locked": bool(ctx["is_locked"]),
        "context_type": ctx["context_type"],
        "blocking": ctx["blocking"],
    }
    if ctx.get("may_be_replaced_by"):
        entry["may_be_replaced_by"] = ctx["may_be_replaced_by"]["record_id"]
    return entry


class CompiledContext(NamedTuple):
    architecture_md: str
    ledger_json: str
    context_ids: list[str]


class ContextFormatterService:
    """Builds a repository's ARCHITECTURE.md and ledger.json from its records."""

    def __init__(self) -> None:
        template_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
        self.env = Environment(loader=FileSystemLoader(template_dir))
        self.template = self.env.get_template("ARCHITECTURE.md.j2")

    async def compile_for_repo(
        self,
        tenant_id: UUID,
        repo_identifier: str,
        store: PostgresEventStore,
        audience: RepoAudience,
        conn: AsyncConnection | None = None,
    ) -> CompiledContext:
        """``conn`` is the store's connection; with it, updates and replacement notes the
        audience may read are included."""
        contexts = [
            ctx
            for ctx in await store.get_active_contexts_for_repo_compilation(
                tenant_id, repo_identifier
            )
            if audience.allows(ctx.get("access_control"))
        ]

        # For each context, fetch symbols and source event links
        formatted_contexts = []
        for ctx in contexts:
            ctx_id = ctx["id"]
            # Fetch symbols
            symbols = await store.get_symbols_for_context_in_repo(
                tenant_id, ctx_id, repo_identifier
            )

            # Fetch source events
            event_ids = await store.get_events_for_context(tenant_id, ctx_id)
            source_link = None
            source_platform = None
            if event_ids:
                events = [
                    e
                    for e in await store.get_events_by_ids(tenant_id, event_ids)
                    if audience.allows(e.access_control.model_dump(mode="json"))
                ]
                if events:
                    event = events[0]
                    source_link = _build_source_link(event)
                    source_platform = (
                        event.source_platform.value
                        if hasattr(event.source_platform, "value")
                        else str(event.source_platform)
                    )

            summary = redaction.serve(ctx["summary"], ctx.get("summary_redacted"))
            title, full_text = heading(summary)
            formatted_contexts.append(
                {
                    "id": str(ctx_id),
                    "summary": summary,
                    "heading": title,
                    "full_text": full_text,
                    "blocking": ctx.get("severity") == BLOCKING,
                    "confidence_score": ctx["confidence_score"],
                    "is_locked": ctx.get("is_locked", False),
                    "context_type": ctx.get("context_type") or "general",
                    "created_at": ctx["created_at"].isoformat() if ctx.get("created_at") else "",
                    "symbols": symbols,
                    "source_link": source_link,
                    "source_platform": source_platform,
                }
            )

        # Group by context_type into typed sections. When every record is
        # "general" the flat rendering is used.
        distinct_types = {ctx["context_type"] for ctx in formatted_contexts}
        use_typed_sections = bool(distinct_types) and distinct_types != {"general"}
        sections: list[dict[str, Any]] = []
        if use_typed_sections:
            by_type: dict[str, list[dict[str, Any]]] = {}
            for ctx in formatted_contexts:
                by_type.setdefault(ctx["context_type"], []).append(ctx)
            for ctype, title in _CONTEXT_TYPE_SECTION_TITLES.items():
                if ctype in by_type:
                    sections.append({"title": title, "contexts": by_type.pop(ctype)})
            # Any future context_type not yet in _CONTEXT_TYPE_SECTION_TITLES
            # still renders, under a title-cased fallback heading, rather
            # than silently dropping it.
            for ctype, ctxs in by_type.items():
                sections.append({"title": ctype.replace("_", " ").title(), "contexts": ctxs})

        if conn is not None:
            await record_notes.annotate(conn, tenant_id, formatted_contexts, allows=audience.allows)
            for ctx in formatted_contexts:
                note = ctx.get("may_be_replaced_by")
                if note:
                    note["date"] = (note["created_at"] or "")[:10]
                for update in ctx["updates"]:
                    update["date"] = (update["happened_at"] or "")[:10]

        # Render ARCHITECTURE.md
        compiled_at = datetime.now(UTC).isoformat()
        architecture_md = self.template.render(
            front_matter=front_matter(
                str(tenant_id), repo_identifier, compiled_at, formatted_contexts
            ),
            notice=NOTICE,
            contexts=formatted_contexts,
            use_typed_sections=use_typed_sections,
            sections=sections,
        )

        # Compile ledger.json
        included = [ctx["id"] for ctx in formatted_contexts]
        mappings = await store.get_repo_ledger_mappings(tenant_id, repo_identifier)
        ledger: dict[str, list[str]] = {}
        for item in mappings:
            sym = item["symbol_name"]
            ctx_id = str(item["canonical_id"])
            if ctx_id not in included:
                continue
            if sym not in ledger:
                ledger[sym] = []
            if ctx_id not in ledger[sym]:
                ledger[sym].append(ctx_id)

        ledger_json = json.dumps(ledger, indent=2)
        return CompiledContext(architecture_md, ledger_json, included)

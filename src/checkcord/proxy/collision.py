import re
from uuid import UUID

import aiosqlite
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.storage.postgres import PostgresEventStore, tenant_context

logger = structlog.get_logger(__name__)

STOPWORDS = {
    "about",
    "above",
    "after",
    "again",
    "against",
    "before",
    "below",
    "between",
    "could",
    "did",
    "do",
    "does",
    "doing",
    "down",
    "during",
    "each",
    "few",
    "for",
    "from",
    "further",
    "had",
    "has",
    "have",
    "having",
    "here",
    "how",
    "if",
    "into",
    "its",
    "itself",
    "more",
    "most",
    "once",
    "only",
    "other",
    "ought",
    "our",
    "ours",
    "theirs",
    "themselves",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "under",
    "until",
    "very",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "whom",
    "why",
    "with",
    "would",
    "your",
    "yours",
    "yourself",
    "yourselves",
    "const",
    "let",
    "var",
    "function",
    "class",
    "import",
    "export",
    "return",
    "default",
    "async",
    "await",
    "public",
    "private",
    "protected",
    "static",
    "package",
    "string",
    "number",
    "boolean",
    "object",
    "array",
    "null",
    "undefined",
    "true",
    "false",
}


def is_camel_or_pascal(s: str) -> bool:
    if not s.isalnum():
        return False
    if len(s) < 3:
        return False
    has_upper = any(c.isupper() for c in s)
    has_lower = any(c.islower() for c in s)
    if not (has_upper and has_lower):
        return False
    return True


def is_snake_case(s: str) -> bool:
    if "_" not in s:
        return False
    if len(s) < 3:
        return False
    if not all(c.isalnum() or c == "_" for c in s):
        return False
    if not any(c.isalnum() for c in s):
        return False
    return True


def extract_symbols_from_payload(payload: dict) -> set[str]:
    """Extract candidate code symbols from prompt JSON payload.
    Only extracts camelCase, PascalCase, or snake_case tokens > 4 characters.
    """
    text_content = []

    # Extract system prompt
    if "system" in payload:
        system_val = payload["system"]
        if isinstance(system_val, str):
            text_content.append(system_val)
        elif isinstance(system_val, list):
            for block in system_val:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_content.append(block.get("text", ""))
                elif isinstance(block, str):
                    text_content.append(block)

    # Extract messages list
    if "messages" in payload and isinstance(payload["messages"], list):
        for msg in payload["messages"]:
            if isinstance(msg, dict) and "content" in msg:
                content = msg["content"]
                if isinstance(content, str):
                    text_content.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_content.append(block.get("text", ""))
                        elif isinstance(block, str):
                            text_content.append(block)

    full_text = " ".join(text_content)
    tokens = re.findall(r"\b[a-zA-Z0-9_]+\b", full_text)

    symbols = set()
    for token in tokens:
        if len(token) < 2:
            continue
        if token.lower() in STOPWORDS:
            continue
        if is_camel_or_pascal(token) or is_snake_case(token):
            symbols.add(token)
            if len(symbols) >= 50:
                break
    return symbols


async def detect_collisions(
    symbols: set[str],
    current_branch: str,
    tenant_id: UUID,
    pg_engine: AsyncEngine,
    ledger_db_path: str,
) -> list[dict]:
    """Detect AST symbols that are modified on parallel branches.

    Returns a list of dicts: [{"symbol_name": ..., "file_path": ..., "branch_name": ...}]
    """
    if not symbols or not tenant_id:
        return []

    # Query Postgres for AST nodes with matching symbol names within this tenant
    matched_ast_nodes = []
    try:
        async with tenant_context(pg_engine, tenant_id) as conn:
            store = PostgresEventStore(conn)
            matched_ast_nodes = await store.find_ast_nodes_by_symbols(tenant_id, list(symbols))
    except Exception as e:
        logger.error("collision_detector.postgres_query_failed", error=str(e))
        return []

    if not matched_ast_nodes:
        return []

    # Map file paths back to their matching symbols, with each symbol's
    # ast_node_id and repo_identifier for the viewer link. First match wins.
    file_to_symbols: dict[str, set[str]] = {}
    symbol_meta: dict[tuple[str, str], dict[str, str]] = {}
    for node in matched_ast_nodes:
        fp = node.get("file_path")
        symbol = node.get("symbol_name")
        if fp and symbol:
            file_to_symbols.setdefault(fp, set()).add(symbol)
            symbol_meta.setdefault(
                (fp, symbol),
                {
                    "ast_node_id": str(node["id"]),
                    "repo_identifier": node["repo_identifier"],
                },
            )

    if not file_to_symbols:
        return []

    # Query SQLite ledger.db for modified files on OTHER active branches
    collisions = []
    try:
        async with aiosqlite.connect(ledger_db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA synchronous=NORMAL;")

            # Find files that are modified on branches other than current_branch
            placeholders = ",".join("?" for _ in file_to_symbols.keys())
            query = f"""
                SELECT branch_name, file_path, status
                FROM modified_files
                WHERE file_path IN ({placeholders})
                  AND branch_name != ?
            """
            params = list(file_to_symbols.keys()) + [current_branch]

            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    branch_name, file_path, status = row
                    matching_symbols = file_to_symbols.get(file_path, set())
                    for sym in matching_symbols:
                        meta = symbol_meta.get((file_path, sym), {})
                        collisions.append(
                            {
                                "symbol_name": sym,
                                "file_path": file_path,
                                "branch_name": branch_name,
                                "status": status,
                                "ast_node_id": meta.get("ast_node_id"),
                                "repo_identifier": meta.get("repo_identifier"),
                            }
                        )
    except Exception as e:
        logger.error("collision_detector.sqlite_query_failed", error=str(e))
        return []

    return collisions

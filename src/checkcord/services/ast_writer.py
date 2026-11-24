"""Shared AST-node write path for initial and ongoing population.

`write_ast_nodes_for_file` is called by the bulk repo backfill and by the
merge-triggered writer. Language detection and vendored/generated-path
exclusion live inside it, so both paths decide what to parse the same way.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from pydantic import BaseModel

from checkcord.ast.hasher import SemanticHasher
from checkcord.ast.parser import ExtractedNode, TreeSitterParser, get_language_from_filename
from checkcord.core.exceptions import ASTParsingError
from checkcord.storage.postgres import PostgresEventStore

logger = structlog.get_logger(__name__).bind(component="ast_writer")

# Vendored, build and generated directories are never parsed or written.
# Path-segment match (not substring), so a file named `my_target_list.py`
# is not excluded by the `target` entry.
_EXCLUDED_PATH_SEGMENTS = frozenset(
    {
        "node_modules",
        "vendor",
        "dist",
        "build",
        ".venv",
        "venv",
        "__pycache__",
        ".git",
        "target",
        ".next",
        "site-packages",
        ".cache",
    }
)


def is_excluded_path(file_path: str) -> bool:
    """True if any path segment matches the vendored/build/generated list."""
    segments = file_path.replace("\\", "/").split("/")
    return any(segment in _EXCLUDED_PATH_SEGMENTS for segment in segments)


class WriteResult(BaseModel):
    """Outcome of writing (or skipping) a single file's AST nodes."""

    file_path: str
    language: str | None = None
    nodes_written: int = 0
    drift_detected_count: int = 0
    skipped: bool = False
    skip_reason: str | None = None


async def write_ast_nodes_for_file(
    tenant_id: UUID,
    repo_identifier: str,
    file_path: str,
    content: bytes,
    store: PostgresEventStore,
    parser: TreeSitterParser,
) -> WriteResult:
    """Extract AST nodes from one file's content and upsert them.

    Idempotent: every write goes through
    ``PostgresEventStore.upsert_ast_node_and_check_drift``, keyed on
    ``(tenant_id, repo_identifier, file_path, symbol_name, node_type)``, so a
    repeat call with unchanged content is a no-op and concurrent calls for the
    same file are safe.

    ``parser`` is passed in so callers processing many files reuse one
    ``TreeSitterParser``; constructing one compiles grammars and is slow.

    Never raises for an unsupported or unparseable file; returns a
    ``WriteResult`` with ``skipped=True`` so one bad file cannot fail a batch.
    """
    if is_excluded_path(file_path):
        logger.debug("ast_writer.excluded_path_skipped", file_path=file_path)
        return WriteResult(file_path=file_path, skipped=True, skip_reason="excluded_path")

    language = get_language_from_filename(file_path)
    if language is None:
        logger.debug("ast_writer.unsupported_language_skipped", file_path=file_path)
        return WriteResult(file_path=file_path, skipped=True, skip_reason="unsupported_language")

    try:
        nodes: list[ExtractedNode] = parser.extract_nodes(content, language)
    except ASTParsingError as e:
        logger.warning("ast_writer.parse_failed", file_path=file_path, error=str(e))
        return WriteResult(
            file_path=file_path,
            language=language,
            skipped=True,
            skip_reason=f"parse_error:{e}",
        )

    hashed_nodes = SemanticHasher.generate_hashes(nodes, language)

    nodes_written = 0
    drift_detected_count = 0
    for node in hashed_nodes:
        drift = await store.upsert_ast_node_and_check_drift(
            tenant_id=tenant_id,
            repo_identifier=repo_identifier,
            file_path=file_path,
            symbol_name=node.symbol_name,
            node_type=node.node_type,
            merkle_hash=node.merkle_hash,
        )
        nodes_written += 1
        if drift:
            drift_detected_count += 1

    return WriteResult(
        file_path=file_path,
        language=language,
        nodes_written=nodes_written,
        drift_detected_count=drift_detected_count,
    )

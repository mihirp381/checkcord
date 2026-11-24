from typing import Any
from uuid import UUID

import structlog

from checkcord.ast.hasher import HashedNode
from checkcord.storage.postgres import PostgresEventStore, normalize_file_path

logger = structlog.get_logger(__name__).bind(component="context_matcher_service")


class ContextMatcherService:
    async def match_contexts(
        self,
        tenant_id: UUID,
        repo_identifier: str,
        file_paths: list[str],
        pr_nodes: list[tuple[str, HashedNode]],
        store: PostgresEventStore,
    ) -> list[dict[str, Any]]:
        """
        Compares PR AST nodes against database AST nodes for modified files.
        Detects:
          - Modified symbols (hash mismatch)
          - Deleted symbols (present in DB for a parsed file, but missing in PR)
        Returns a list of dicts describing the affected contexts:
          [
            {
              "symbol_name": str,
              "file_path": str,
              "node_type": str,
              "change_type": "modified" | "deleted",
              "canonical_ids": list of UUID strings
            }
          ]
        """
        if not file_paths:
            return []

        # Fetch DB nodes and active edges for these files
        db_rows = await store.find_ast_nodes_by_paths(
            tenant_id=tenant_id,
            repo_identifier=repo_identifier,
            file_paths=file_paths,
        )

        # Group DB nodes by normalized file_path and symbol identifier
        # DB row: {"ast_node_id", "file_path", "symbol_name", "node_type",
        # "merkle_hash", "canonical_id"}
        # Map: (normalized_path, symbol_name, node_type) -> {"merkle_hash": str,
        # "canonical_ids": set[UUID]}
        db_nodes: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in db_rows:
            db_path = row["file_path"]
            symbol = row["symbol_name"]
            node_type = row["node_type"]
            m_hash = row["merkle_hash"]
            canonical_id = row["canonical_id"]

            key = (db_path, symbol, node_type)
            if key not in db_nodes:
                db_nodes[key] = {
                    "merkle_hash": m_hash,
                    "canonical_ids": set(),
                }
            if canonical_id:
                db_nodes[key]["canonical_ids"].add(canonical_id)

        # Group PR nodes by normalized file_path and symbol identifier
        # Map: (normalized_path, symbol_name, node_type) -> HashedNode
        pr_nodes_map: dict[tuple[str, str, str], HashedNode] = {}
        for path, node in pr_nodes:
            norm_path = normalize_file_path(path, repo_identifier)
            key = (norm_path, node.symbol_name, node.node_type)
            pr_nodes_map[key] = node

        affected_contexts: list[dict[str, Any]] = []

        # Check for modifications (and drift)
        for key, pr_node in pr_nodes_map.items():
            norm_path, symbol_name, node_type = key
            if key in db_nodes:
                db_node = db_nodes[key]
                if db_node["merkle_hash"] != pr_node.merkle_hash:
                    # Hash mismatch detected (drift)
                    # If there are active canonical contexts linked, they are affected
                    active_ids = db_node["canonical_ids"]
                    if active_ids:
                        affected_contexts.append(
                            {
                                "symbol_name": symbol_name,
                                "file_path": norm_path,
                                "node_type": node_type,
                                "change_type": "modified",
                                "canonical_ids": [str(cid) for cid in active_ids],
                            }
                        )
                        logger.info(
                            "context_drift_detected",
                            symbol=symbol_name,
                            file_path=norm_path,
                            change_type="modified",
                            canonical_ids_count=len(active_ids),
                        )

        # Check for deletions
        # Any DB node in the modified files that is NOT present in the PR nodes is deleted
        normalized_target_paths = {normalize_file_path(p, repo_identifier) for p in file_paths}
        for key, db_node in db_nodes.items():
            norm_path, symbol_name, node_type = key
            # Only check deletion for files that were actually parsed/analyzed in the PR
            if norm_path in normalized_target_paths:
                if key not in pr_nodes_map:
                    active_ids = db_node["canonical_ids"]
                    if active_ids:
                        affected_contexts.append(
                            {
                                "symbol_name": symbol_name,
                                "file_path": norm_path,
                                "node_type": node_type,
                                "change_type": "deleted",
                                "canonical_ids": [str(cid) for cid in active_ids],
                            }
                        )
                        logger.info(
                            "context_drift_detected",
                            symbol=symbol_name,
                            file_path=norm_path,
                            change_type="deleted",
                            canonical_ids_count=len(active_ids),
                        )

        return affected_contexts

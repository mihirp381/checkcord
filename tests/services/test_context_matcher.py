"""Unit tests for ContextMatcherService.

The drift-detection core of the Semantic CI/CD PR Blocker.
"""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from checkcord.ast.hasher import HashedNode
from checkcord.services.context_matcher import ContextMatcherService

pytestmark = pytest.mark.asyncio


def make_hashed_node(
    symbol_name: str, merkle_hash: str, node_type: str = "function_definition"
) -> HashedNode:
    return HashedNode(
        node_type=node_type,
        symbol_name=symbol_name,
        start_byte=0,
        end_byte=10,
        content=f"def {symbol_name}(): pass",
        merkle_hash=merkle_hash,
    )


async def test_empty_file_paths_returns_empty_list() -> None:
    service = ContextMatcherService()
    store = AsyncMock()

    result = await service.match_contexts(
        tenant_id=uuid4(),
        repo_identifier="acme/repo",
        file_paths=[],
        pr_nodes=[],
        store=store,
    )

    assert result == []
    store.find_ast_nodes_by_paths.assert_not_called()


async def test_modified_symbol_with_active_context_is_flagged() -> None:
    """A hash mismatch on a symbol with linked canonical contexts must be reported as 'modified'."""
    service = ContextMatcherService()
    canonical_id = uuid4()
    store = AsyncMock()
    store.find_ast_nodes_by_paths.return_value = [
        {
            "ast_node_id": uuid4(),
            "file_path": "src/auth.py",
            "symbol_name": "verify_token",
            "node_type": "function_definition",
            "merkle_hash": "old_hash",
            "canonical_id": canonical_id,
        }
    ]

    pr_node = make_hashed_node("verify_token", merkle_hash="new_hash")

    result = await service.match_contexts(
        tenant_id=uuid4(),
        repo_identifier="acme/repo",
        file_paths=["src/auth.py"],
        pr_nodes=[("src/auth.py", pr_node)],
        store=store,
    )

    assert len(result) == 1
    assert result[0]["symbol_name"] == "verify_token"
    assert result[0]["change_type"] == "modified"
    assert result[0]["canonical_ids"] == [str(canonical_id)]


async def test_modified_symbol_without_active_context_is_not_flagged() -> None:
    """A hash mismatch with no linked canonical contexts is a no-op — nothing to warn about."""
    service = ContextMatcherService()
    store = AsyncMock()
    store.find_ast_nodes_by_paths.return_value = [
        {
            "ast_node_id": uuid4(),
            "file_path": "src/auth.py",
            "symbol_name": "verify_token",
            "node_type": "function_definition",
            "merkle_hash": "old_hash",
            "canonical_id": None,
        }
    ]

    pr_node = make_hashed_node("verify_token", merkle_hash="new_hash")

    result = await service.match_contexts(
        tenant_id=uuid4(),
        repo_identifier="acme/repo",
        file_paths=["src/auth.py"],
        pr_nodes=[("src/auth.py", pr_node)],
        store=store,
    )

    assert result == []


async def test_unchanged_symbol_is_not_flagged() -> None:
    """Matching hashes mean no drift — must not appear in the affected list."""
    service = ContextMatcherService()
    canonical_id = uuid4()
    store = AsyncMock()
    store.find_ast_nodes_by_paths.return_value = [
        {
            "ast_node_id": uuid4(),
            "file_path": "src/auth.py",
            "symbol_name": "verify_token",
            "node_type": "function_definition",
            "merkle_hash": "same_hash",
            "canonical_id": canonical_id,
        }
    ]

    pr_node = make_hashed_node("verify_token", merkle_hash="same_hash")

    result = await service.match_contexts(
        tenant_id=uuid4(),
        repo_identifier="acme/repo",
        file_paths=["src/auth.py"],
        pr_nodes=[("src/auth.py", pr_node)],
        store=store,
    )

    assert result == []


async def test_deleted_symbol_with_active_context_is_flagged() -> None:
    """A DB-known symbol absent from the PR's parsed nodes (for an analyzed file) is 'deleted'."""
    service = ContextMatcherService()
    canonical_id = uuid4()
    store = AsyncMock()
    store.find_ast_nodes_by_paths.return_value = [
        {
            "ast_node_id": uuid4(),
            "file_path": "src/auth.py",
            "symbol_name": "old_helper",
            "node_type": "function_definition",
            "merkle_hash": "hash_x",
            "canonical_id": canonical_id,
        }
    ]

    result = await service.match_contexts(
        tenant_id=uuid4(),
        repo_identifier="acme/repo",
        file_paths=["src/auth.py"],
        pr_nodes=[],
        store=store,
    )

    assert len(result) == 1
    assert result[0]["symbol_name"] == "old_helper"
    assert result[0]["change_type"] == "deleted"
    assert result[0]["canonical_ids"] == [str(canonical_id)]


async def test_deleted_symbol_in_unanalyzed_file_is_not_flagged() -> None:
    """A DB symbol in a file outside this PR's changed file_paths must not be treated as deleted."""
    service = ContextMatcherService()
    canonical_id = uuid4()
    store = AsyncMock()
    store.find_ast_nodes_by_paths.return_value = [
        {
            "ast_node_id": uuid4(),
            "file_path": "src/unrelated.py",
            "symbol_name": "some_symbol",
            "node_type": "function_definition",
            "merkle_hash": "hash_x",
            "canonical_id": canonical_id,
        }
    ]

    result = await service.match_contexts(
        tenant_id=uuid4(),
        repo_identifier="acme/repo",
        file_paths=["src/auth.py"],
        pr_nodes=[],
        store=store,
    )

    assert result == []

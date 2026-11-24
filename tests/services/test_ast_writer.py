from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from checkcord.ast.parser import TreeSitterParser
from checkcord.services.ast_writer import is_excluded_path, write_ast_nodes_for_file
from checkcord.storage.postgres import PostgresEventStore


def test_is_excluded_path() -> None:
    assert is_excluded_path("node_modules/foo/bar.js") is True
    assert is_excluded_path("src/vendor/lib.py") is True
    assert is_excluded_path("build/output.py") is True
    assert is_excluded_path("dist/bundle.js") is True
    assert is_excluded_path(".venv/lib/site-packages/foo.py") is True
    assert is_excluded_path("__pycache__/foo.cpython-312.pyc") is True
    assert is_excluded_path("src/checkcord/main.py") is False
    # Substring, not segment, match must not false-positive.
    assert is_excluded_path("src/my_target_list.py") is False
    assert is_excluded_path("src/distinct_module.py") is False


@pytest.mark.asyncio
async def test_write_ast_nodes_for_file_excluded_path_is_skipped() -> None:
    store = AsyncMock(spec=PostgresEventStore)
    parser = TreeSitterParser()

    result = await write_ast_nodes_for_file(
        tenant_id=uuid4(),
        repo_identifier="owner/repo",
        file_path="node_modules/foo/bar.py",
        content=b"def f():\n    pass\n",
        store=store,
        parser=parser,
    )

    assert result.skipped is True
    assert result.skip_reason == "excluded_path"
    store.upsert_ast_node_and_check_drift.assert_not_called()


@pytest.mark.asyncio
async def test_write_ast_nodes_for_file_unsupported_language_is_skipped() -> None:
    store = AsyncMock(spec=PostgresEventStore)
    parser = TreeSitterParser()

    result = await write_ast_nodes_for_file(
        tenant_id=uuid4(),
        repo_identifier="owner/repo",
        file_path="README.md",
        content=b"# Hello\n",
        store=store,
        parser=parser,
    )

    assert result.skipped is True
    assert result.skip_reason == "unsupported_language"
    store.upsert_ast_node_and_check_drift.assert_not_called()


@pytest.mark.asyncio
async def test_write_ast_nodes_for_file_writes_extracted_nodes() -> None:
    tenant_id = uuid4()
    store = AsyncMock(spec=PostgresEventStore)
    store.upsert_ast_node_and_check_drift.return_value = False
    parser = TreeSitterParser()

    content = b"def hello_world():\n    pass\n"
    result = await write_ast_nodes_for_file(
        tenant_id=tenant_id,
        repo_identifier="owner/repo",
        file_path="src/main.py",
        content=content,
        store=store,
        parser=parser,
    )

    assert result.skipped is False
    assert result.language == "python"
    assert result.nodes_written == 1
    assert result.drift_detected_count == 0

    store.upsert_ast_node_and_check_drift.assert_called_once()
    call_kwargs = store.upsert_ast_node_and_check_drift.call_args.kwargs
    assert call_kwargs["tenant_id"] == tenant_id
    assert call_kwargs["repo_identifier"] == "owner/repo"
    assert call_kwargs["file_path"] == "src/main.py"
    assert call_kwargs["symbol_name"] == "hello_world"
    assert call_kwargs["node_type"] == "function_definition"
    assert isinstance(call_kwargs["merkle_hash"], str)
    assert len(call_kwargs["merkle_hash"]) == 64


@pytest.mark.asyncio
async def test_write_ast_nodes_for_file_reports_drift() -> None:
    store = AsyncMock(spec=PostgresEventStore)
    store.upsert_ast_node_and_check_drift.return_value = True
    parser = TreeSitterParser()

    result = await write_ast_nodes_for_file(
        tenant_id=uuid4(),
        repo_identifier="owner/repo",
        file_path="src/main.py",
        content=b"def hello_world():\n    pass\n",
        store=store,
        parser=parser,
    )

    assert result.nodes_written == 1
    assert result.drift_detected_count == 1


@pytest.mark.asyncio
async def test_write_ast_nodes_for_file_multiple_nodes_java() -> None:
    store = AsyncMock(spec=PostgresEventStore)
    store.upsert_ast_node_and_check_drift.return_value = False
    parser = TreeSitterParser()

    content = (
        b"public class Greeter {\n"
        b"    public String greet(String name) {\n"
        b'        return "hello " + name;\n'
        b"    }\n"
        b"}\n"
    )
    result = await write_ast_nodes_for_file(
        tenant_id=uuid4(),
        repo_identifier="owner/repo",
        file_path="src/Greeter.java",
        content=content,
        store=store,
        parser=parser,
    )

    assert result.language == "java"
    # One class_definition + one method_definition.
    assert result.nodes_written == 2
    assert store.upsert_ast_node_and_check_drift.call_count == 2


@pytest.mark.asyncio
async def test_write_ast_nodes_for_file_parse_error_is_skipped_not_raised() -> None:
    """A single unparseable file must not raise -- the containing batch
    (RepoBackfillWorker/ASTWriterWorker) must be able to continue past it."""
    store = AsyncMock(spec=PostgresEventStore)
    parser = TreeSitterParser()

    # tree-sitter is generally tolerant of malformed source (it produces an
    # error node rather than raising), so force the failure path directly by
    # feeding content that can't be decoded as the language expects at all --
    # simplest reliable trigger is monkeypatching extract_nodes to raise.
    from checkcord.core.exceptions import ASTParsingError

    def _boom(*args: object, **kwargs: object) -> None:
        raise ASTParsingError("simulated parse failure")

    parser.extract_nodes = _boom  # type: ignore[method-assign]

    result = await write_ast_nodes_for_file(
        tenant_id=uuid4(),
        repo_identifier="owner/repo",
        file_path="src/broken.py",
        content=b"def f(:\n",
        store=store,
        parser=parser,
    )

    assert result.skipped is True
    assert result.skip_reason is not None
    assert "parse_error" in result.skip_reason
    store.upsert_ast_node_and_check_drift.assert_not_called()

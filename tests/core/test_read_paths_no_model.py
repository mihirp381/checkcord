import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "checkcord"

READ_MODULES = [
    "api/graph_router.py",
    "api/admin_router.py",
    "api/agent_ledger_router.py",
    "services/mcp_tools.py",
    "proxy/context.py",
    "services/records.py",
    "services/review.py",
    "services/context_formatter.py",
    "workers/deprecation_worker.py",
    "core/redaction.py",
    "services/record_notes.py",
]
MODEL_MODULES = ("checkcord.pipeline.redaction", "checkcord.llm.factory", "checkcord.llm.client")


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


@pytest.mark.parametrize("module", READ_MODULES)
def test_read_module_does_not_import_a_chat_model(module: str) -> None:
    imported = _imports(SRC / module)
    assert not [m for m in imported if m.startswith(MODEL_MODULES)]
    assert "redact_text" not in (SRC / module).read_text()


def test_the_pr_check_calls_a_model_only_to_judge() -> None:
    path = SRC / "workers/pr_status_reporter.py"
    imported = _imports(path)
    assert not [m for m in imported if m.startswith("checkcord.pipeline.redaction")]
    assert "redact_text" not in path.read_text()

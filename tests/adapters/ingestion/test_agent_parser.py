"""Tests for the agent trajectory parser."""

from checkcord.adapters.ingestion.agent_parser import (
    _normalise_path,
    build_entity_references,
    extract_file_paths_from_raw,
    extract_file_paths_from_steps,
    extract_tool_invocations,
    parse_raw_transcript_to_steps,
    parse_trajectory,
)
from checkcord.schemas.agent import (
    AgentToolCall,
    AgentTrajectoryPayload,
    AgentTrajectoryStep,
)


def test_normalise_path_absolute() -> None:
    """Absolute paths get file:// prefix."""
    assert _normalise_path("/Users/dev/src/auth.py") == "file:///Users/dev/src/auth.py"


def test_normalise_path_strips_quotes() -> None:
    """Quoted paths are cleaned."""
    assert _normalise_path("'/tmp/test.py'") == "file:///tmp/test.py"


def test_normalise_path_relative() -> None:
    """Relative paths still get a file:// prefix."""
    result = _normalise_path("src/auth.py")
    assert result.startswith("file://")


def test_extract_file_paths_from_steps() -> None:
    """File paths are extracted from tool call arguments."""
    steps = [
        AgentTrajectoryStep(
            step_index=0,
            type="PLANNER_RESPONSE",
            tool_calls=[
                AgentToolCall(
                    tool_name="view_file",
                    arguments={"AbsolutePath": "/src/auth.py"},
                ),
                AgentToolCall(
                    tool_name="replace_file_content",
                    arguments={"TargetFile": "/src/db.py"},
                ),
            ],
        ),
        AgentTrajectoryStep(
            step_index=1,
            type="PLANNER_RESPONSE",
            tool_calls=[
                AgentToolCall(
                    tool_name="run_command",
                    arguments={"CommandLine": "pytest"},
                ),
            ],
        ),
    ]

    paths = extract_file_paths_from_steps(steps)
    assert "/src/auth.py" in paths
    assert "/src/db.py" in paths
    assert len(paths) == 2


def test_extract_file_paths_from_raw() -> None:
    """File paths are extracted from raw text."""
    raw = """
    I read the file /Users/dev/project/src/auth.py and found the issue.
    Then I modified /Users/dev/project/src/login.py to fix the bug.
    Also check pyproject.toml and src/db.py.
    """
    paths = extract_file_paths_from_raw(raw)
    assert "/Users/dev/project/src/auth.py" in paths
    assert "/Users/dev/project/src/login.py" in paths
    assert "pyproject.toml" in paths
    assert "src/db.py" in paths


def test_extract_tool_invocations() -> None:
    """Unique tool names are extracted from steps."""
    steps = [
        AgentTrajectoryStep(
            step_index=0,
            tool_calls=[
                AgentToolCall(tool_name="view_file", arguments={}),
                AgentToolCall(tool_name="run_command", arguments={}),
            ],
        ),
        AgentTrajectoryStep(
            step_index=1,
            tool_calls=[
                AgentToolCall(tool_name="view_file", arguments={}),
                AgentToolCall(tool_name="write_to_file", arguments={}),
            ],
        ),
    ]
    invocations = extract_tool_invocations(steps)
    assert invocations == ["view_file", "run_command", "write_to_file"]


def test_build_entity_references() -> None:
    """File paths are converted to EntityReference objects with file:// URIs."""
    paths = {"/src/auth.py", "/src/db.py"}
    refs = build_entity_references(paths)
    assert len(refs) == 2
    for ref in refs:
        assert ref.entity_type == "file"
        assert ref.entity_id.startswith("file:///")
        assert ref.entity_platform == "filesystem"
        assert ref.relationship == "context"


def test_parse_trajectory_with_steps() -> None:
    """Full trajectory parsing with structured steps."""
    payload = AgentTrajectoryPayload(
        agent_name="test-agent",
        session_id="sess-001",
        repository="org/repo",
        task_description="Fix authentication bug",
        steps=[
            AgentTrajectoryStep(
                step_index=0,
                type="USER_INPUT",
                content="Fix the auth bug in the login module",
            ),
            AgentTrajectoryStep(
                step_index=1,
                type="PLANNER_RESPONSE",
                content="I'll check the auth module.",
                tool_calls=[
                    AgentToolCall(
                        tool_name="view_file",
                        arguments={"AbsolutePath": "/src/auth.py"},
                    ),
                    AgentToolCall(
                        tool_name="replace_file_content",
                        arguments={"TargetFile": "/src/login.py"},
                    ),
                ],
            ),
        ],
    )

    content_raw, content_clean, entity_refs, meta = parse_trajectory(payload)

    # Content contains agent session info
    assert "test-agent" in content_raw
    assert "sess-001" in content_raw
    assert "Fix authentication bug" in content_raw

    # Entity references for files
    assert len(entity_refs) == 2
    file_ids = {ref.entity_id for ref in entity_refs}
    assert "file:///src/auth.py" in file_ids
    assert "file:///src/login.py" in file_ids

    # Metadata
    assert "view_file" in meta["tool_invocations"]
    assert "replace_file_content" in meta["tool_invocations"]
    assert "/src/auth.py" in meta["file_paths"]
    assert "/src/login.py" in meta["file_paths"]


def test_parse_trajectory_with_raw_transcript() -> None:
    """Full trajectory parsing with raw text transcript."""
    payload = AgentTrajectoryPayload(
        agent_name="markdown-agent",
        raw_transcript=(
            "# Session Log\n"
            "Read /Users/dev/project/src/utils.py for helpers.\n"
            "Modified /Users/dev/project/src/main.py to fix issue.\n"
        ),
    )

    content_raw, content_clean, entity_refs, meta = parse_trajectory(payload)
    assert "markdown-agent" in content_raw
    assert len(entity_refs) >= 2
    assert len(meta["file_paths"]) >= 2


def test_parse_trajectory_requires_content() -> None:
    """Payload with neither steps nor raw_transcript still parses
    (validation is done at the router level)."""
    payload = AgentTrajectoryPayload(agent_name="empty-agent")
    content_raw, content_clean, entity_refs, meta = parse_trajectory(payload)
    assert "empty-agent" in content_raw
    assert len(entity_refs) == 0


def test_normalise_path_already_prefixed() -> None:
    """Paths with a file:// prefix are normalised without double-prefixing."""
    assert _normalise_path("file:///Users/dev/src/auth.py") == "file:///Users/dev/src/auth.py"
    assert _normalise_path("file://src/auth.py") == "file:///src/auth.py"


def test_extract_file_paths_from_command_args() -> None:
    """Paths are extracted from command-line arguments like CommandLine or command."""
    steps = [
        AgentTrajectoryStep(
            step_index=0,
            tool_calls=[
                AgentToolCall(
                    tool_name="run_command",
                    arguments={"CommandLine": "cat /src/auth.py && ruff check src/login.py"},
                ),
                AgentToolCall(
                    tool_name="execute_command",
                    arguments={"command": "pytest tests/api/test_agent_trajectory.py"},
                ),
            ],
        )
    ]
    paths = extract_file_paths_from_steps(steps)
    assert "/src/auth.py" in paths
    assert "src/login.py" in paths
    assert "tests/api/test_agent_trajectory.py" in paths


def test_parse_raw_transcript_json_array() -> None:
    """Parsing a raw transcript that is a JSON array of step objects."""
    raw = (
        "[\n"
        '  {"step_index": 0, "type": "USER_INPUT", "content": "Init"},\n'
        '  {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "Step 2"}\n'
        "]"
    )
    steps = parse_raw_transcript_to_steps(raw)
    assert len(steps) == 2
    assert steps[0].content == "Init"
    assert steps[1].content == "Step 2"


def test_parse_raw_transcript_json_object_with_key() -> None:
    """Parsing a raw transcript that is a JSON object with a steps key."""
    raw = '{\n  "steps": [\n    {"step_index": 0, "content": "Hello"}\n  ]\n}'
    steps = parse_raw_transcript_to_steps(raw)
    assert len(steps) == 1
    assert steps[0].content == "Hello"


def test_parse_raw_transcript_jsonl() -> None:
    """Parsing a raw transcript that is JSONL formatted."""
    raw = '{"step_index": 0, "content": "Line 1"}\n{"step_index": 1, "content": "Line 2"}\n'
    steps = parse_raw_transcript_to_steps(raw)
    assert len(steps) == 2
    assert steps[0].content == "Line 1"
    assert steps[1].content == "Line 2"


def test_parse_raw_transcript_markdown_ignored() -> None:
    """Parsing a raw transcript that is markdown returns an empty list of steps."""
    raw = "# Title\nThis is a markdown file with /src/auth.py in it."
    steps = parse_raw_transcript_to_steps(raw)
    assert steps == []


def test_parse_trajectory_with_json_transcript() -> None:
    """Full parse of trajectory where raw_transcript contains a JSON array of steps."""
    payload = AgentTrajectoryPayload(
        agent_name="json-agent",
        raw_transcript=(
            "[\n"
            "  {\n"
            '    "step_index": 0,\n'
            '    "tool_calls": [\n'
            '      {"tool_name": "view_file", "arguments": {"AbsolutePath": "/src/auth.py"}}\n'
            "    ]\n"
            "  }\n"
            "]"
        ),
    )
    content_raw, content_clean, entity_refs, meta = parse_trajectory(payload)
    assert "json-agent" in content_raw
    assert len(entity_refs) == 1
    assert entity_refs[0].entity_id == "file:///src/auth.py"

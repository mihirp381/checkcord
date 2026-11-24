"""Tool calls and file paths from agent session transcripts."""

import json
import re

import structlog

from checkcord.models.common import EntityReference
from checkcord.schemas.agent import AgentToolCall, AgentTrajectoryPayload, AgentTrajectoryStep

logger = structlog.get_logger(__name__).bind(component="agent_parser")

# Tool names whose arguments contain file paths we want to extract
FILE_TOOL_NAMES = frozenset(
    {
        "view_file",
        "read_file",
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "create_file",
        "grep_search",
        "view_file_outline",
        "show_symbols",
        "get_outline",
        "open_file",
        "save_file",
        "edit_file",
    }
)

# Argument keys that contain file paths
FILE_ARG_KEYS = frozenset(
    {
        "AbsolutePath",
        "TargetFile",
        "SearchPath",
        "path",
        "file_path",
        "filepath",
        "file",
        "filename",
        "fileName",
        "source_file",
        "dest_file",
        "output_file",
        "input_file",
    }
)

COMMAND_ARG_KEYS = frozenset({"CommandLine", "command", "cmd", "args"})

# Regex to find file paths in raw text (both absolute and relative POSIX paths)
# Matches paths starting with / or containing at least one slash, ending with common extensions.
# Also matches single files with code extensions if they are enclosed in backticks or quotes,
# or stand alone.
RAW_FILE_PATH_PATTERN = re.compile(
    r"(?:^|[\s\"'`(])("
    r"(?:/[a-zA-Z0-9_\-./]+|(?:\b[a-zA-Z0-9_\-.]+/[a-zA-Z0-9_\-./]+))"
    r"\.(?:py|ts|js|go|rs|java|rb|c|cpp|h|hpp|md|txt|yaml|yml"
    r"|json|toml|cfg|ini|sh|sql|html|css|proto|lock|ini|env)"
    r"|(?:\b[a-zA-Z0-9_\-.]+\.(?:py|ts|js|go|rs|java|rb|c|cpp|h|hpp|md|yaml|yml|toml|proto|lock))\b"
    r")",
)


def _normalise_path(raw_path: str) -> str:
    """Normalise a file path to a canonical file:// URI.

    Strips relative segments (../) and ensures consistent formatting.
    """
    # Remove surrounding quotes / whitespace
    cleaned = raw_path.strip().strip("\"'`")
    if not cleaned:
        return ""

    # Strip existing file:// prefix if present
    if cleaned.startswith("file://"):
        cleaned = cleaned[7:]

    # Resolve relative path segments
    try:
        import posixpath

        resolved = posixpath.normpath(cleaned)
    except Exception:
        resolved = cleaned

    # Ensure file:/// prefix (always 3 slashes for absolute/relative normalized paths)
    if resolved.startswith("/"):
        return f"file://{resolved}"
    return f"file:///{resolved}"


def extract_file_paths_from_steps(
    steps: list[AgentTrajectoryStep],
) -> set[str]:
    """Extract unique file paths from structured transcript steps."""
    paths: set[str] = set()

    for step in steps:
        for tc in step.tool_calls:
            # Check all arguments
            for arg_key, val in tc.arguments.items():
                is_known_file_key = arg_key in FILE_ARG_KEYS
                is_command_key = arg_key in COMMAND_ARG_KEYS
                is_file_tool = tc.tool_name in FILE_TOOL_NAMES

                if isinstance(val, str) and val.strip():
                    cleaned_val = val.strip()
                    if is_command_key:
                        # Extract paths from the command line string using regex
                        paths.update(extract_file_paths_from_raw(cleaned_val))
                    elif is_known_file_key or is_file_tool:
                        # If it's a file tool but not a known key, verify it's a path or file ref
                        if not is_known_file_key:
                            # Accept if starts with / or has an extension in final segment
                            if not (
                                cleaned_val.startswith("/") or "." in cleaned_val.split("/")[-1]
                            ):
                                continue
                        paths.add(cleaned_val)
                    elif tc.tool_name in ("run_command", "execute_command"):
                        # Fallback for run_command arguments that aren't explicitly command keys
                        paths.update(extract_file_paths_from_raw(cleaned_val))

    return paths


def extract_file_paths_from_raw(raw_text: str) -> set[str]:
    """Extract unique file paths from raw/markdown transcript text."""
    paths: set[str] = set()
    for match in RAW_FILE_PATH_PATTERN.finditer(raw_text):
        candidate = match.group(1).strip()
        if candidate:
            paths.add(candidate)
    return paths


def build_content_summary(
    payload: AgentTrajectoryPayload,
    steps: list[AgentTrajectoryStep],
    extracted_paths: set[str],
    tool_invocations: list[str],
) -> tuple[str, str]:
    """Build content_raw and content_clean from the trajectory payload.

    Returns (content_raw, content_clean).
    """
    parts_raw: list[str] = []
    parts_clean: list[str] = []

    # Header
    agent_name = payload.agent_name or "Unknown Agent"
    session_id = payload.session_id or "N/A"
    task_desc = payload.task_description or "No task description"
    repo = payload.repository or "N/A"

    header = (
        f"Agent Session: {agent_name}\n"
        f"Session ID: {session_id}\n"
        f"Repository: {repo}\n"
        f"Task: {task_desc}"
    )
    parts_raw.append(header)
    parts_clean.append(header)

    # Structured steps content
    if steps:
        step_summaries: list[str] = []
        for step in steps:
            # Format steps with step_index and role/source if available
            role_str = f"[{step.source or 'Step'}] " if step.source else ""
            content_str = step.content or ""
            if step.thinking:
                content_str = f"Thinking: {step.thinking}\n{content_str}"
            if content_str:
                step_summaries.append(f"{role_str}{content_str}")
        if step_summaries:
            raw_body = "\n\n---\n\n".join(step_summaries)
            parts_raw.append(raw_body)
            # Clean version: just the text content, no separators
            parts_clean.append("\n".join(step_summaries))

    # Raw transcript (only if we did NOT parse steps from it, to avoid double-dumping)
    if payload.raw_transcript and not (payload.steps or (steps and not payload.steps)):
        parts_raw.append(payload.raw_transcript)
        parts_clean.append(payload.raw_transcript)

    # Tool invocation summary
    if tool_invocations:
        tools_section = "Tool Invocations:\n" + "\n".join(f"  - {t}" for t in tool_invocations)
        parts_clean.append(tools_section)

    # File context summary
    if extracted_paths:
        files_section = "Files Referenced:\n" + "\n".join(
            f"  - {p}" for p in sorted(extracted_paths)
        )
        parts_clean.append(files_section)

    content_raw = "\n\n".join(parts_raw)
    content_clean = "\n\n".join(parts_clean)

    return content_raw, content_clean


def extract_tool_invocations(
    steps: list[AgentTrajectoryStep],
) -> list[str]:
    """Extract a deduplicated list of tool invocation descriptions."""
    invocations: list[str] = []
    seen: set[str] = set()

    for step in steps:
        for tc in step.tool_calls:
            key = tc.tool_name
            if key not in seen:
                seen.add(key)
                invocations.append(key)

    return invocations


def build_entity_references(
    file_paths: set[str],
) -> list[EntityReference]:
    """Convert extracted file paths into EntityReference objects."""
    refs: list[EntityReference] = []
    seen: set[str] = set()

    for raw_path in sorted(file_paths):
        uri = _normalise_path(raw_path)
        if uri and uri not in seen:
            seen.add(uri)
            refs.append(
                EntityReference(
                    entity_type="file",
                    entity_id=uri,
                    entity_platform="filesystem",
                    relationship="context",
                )
            )

    return refs


def parse_raw_transcript_to_steps(raw_text: str) -> list[AgentTrajectoryStep]:
    """Parse a transcript given as a JSON object with `steps`, a JSON array or JSON Lines."""
    cleaned = raw_text.strip()
    if not cleaned:
        return []

    # Try parsing as a single JSON object/array
    if cleaned.startswith("{") or cleaned.startswith("["):
        try:
            data = json.loads(cleaned)
            if isinstance(data, list):
                steps: list[AgentTrajectoryStep] = []
                for item in data:
                    if isinstance(item, dict):
                        # Convert arguments in tool_calls to dict if present
                        if "tool_calls" in item and isinstance(item["tool_calls"], list):
                            tcs = []
                            for tc in item["tool_calls"]:
                                if isinstance(tc, dict):
                                    tcs.append(AgentToolCall(**tc))
                            item["tool_calls"] = tcs
                        steps.append(AgentTrajectoryStep(**item))
                return steps
            elif isinstance(data, dict):
                # Check for common step container keys
                for key in ("steps", "trajectory", "history", "logs"):
                    steps_list = data.get(key)
                    if isinstance(steps_list, list):
                        steps = []
                        for item in steps_list:
                            if isinstance(item, dict):
                                if "tool_calls" in item and isinstance(item["tool_calls"], list):
                                    tcs = []
                                    for tc in item["tool_calls"]:
                                        if isinstance(tc, dict):
                                            tcs.append(AgentToolCall(**tc))
                                    item["tool_calls"] = tcs
                                steps.append(AgentTrajectoryStep(**item))
                        return steps
                # Maybe the object itself is a single step
                if "tool_calls" in data and isinstance(data["tool_calls"], list):
                    tcs = []
                    for tc in data["tool_calls"]:
                        if isinstance(tc, dict):
                            tcs.append(AgentToolCall(**tc))
                    data["tool_calls"] = tcs
                return [AgentTrajectoryStep(**data)]
        except Exception as e:
            logger.debug("agent_trajectory_raw_json_parse_failed", error=str(e))

    # Try parsing as JSONL
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if lines and lines[0].startswith("{"):
        steps = []
        for i, line in enumerate(lines):
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    if "tool_calls" in data and isinstance(data["tool_calls"], list):
                        tcs = []
                        for tc in data["tool_calls"]:
                            if isinstance(tc, dict):
                                tcs.append(AgentToolCall(**tc))
                        data["tool_calls"] = tcs
                    steps.append(AgentTrajectoryStep(**data))
            except Exception as e:
                logger.debug(
                    "agent_trajectory_raw_jsonl_parse_line_skip",
                    line_index=i,
                    error=str(e),
                )
        if steps:
            return steps

    return []


def parse_trajectory(
    payload: AgentTrajectoryPayload,
) -> tuple[str, str, list[EntityReference], dict[str, list[str]]]:
    """Parse an agent trajectory payload into structured outputs.

    Returns:
        (content_raw, content_clean, entity_references, extraction_meta)

    extraction_meta contains:
        - "tool_invocations": list of unique tool names invoked
        - "file_paths": list of extracted file paths
    """
    all_file_paths: set[str] = set()
    tool_invocations: list[str] = []

    # Process structured steps from payload
    steps = list(payload.steps) if payload.steps else []

    # If steps are not provided directly, try to parse raw_transcript into steps
    if not steps and payload.raw_transcript:
        steps = parse_raw_transcript_to_steps(payload.raw_transcript)

    # Process steps (either provided directly or parsed from raw_transcript)
    if steps:
        all_file_paths.update(extract_file_paths_from_steps(steps))
        tool_invocations = extract_tool_invocations(steps)
        # Also extract paths from step content and thinking (treating them as raw text)
        for step in steps:
            if step.content:
                all_file_paths.update(extract_file_paths_from_raw(step.content))
            if step.thinking:
                all_file_paths.update(extract_file_paths_from_raw(step.thinking))

    # Process raw transcript if steps were not successfully parsed
    if payload.raw_transcript and not steps:
        all_file_paths.update(extract_file_paths_from_raw(payload.raw_transcript))

    # Build entity references
    entity_refs = build_entity_references(all_file_paths)

    # Build content summary
    content_raw, content_clean = build_content_summary(
        payload, steps, all_file_paths, tool_invocations
    )

    extraction_meta = {
        "tool_invocations": tool_invocations,
        "file_paths": sorted(all_file_paths),
    }

    return content_raw, content_clean, entity_refs, extraction_meta

from pathlib import Path

from checkcord.eval.golden_set import (
    GoldenSetRow,
    filter_extracted_signal,
    load_golden_set,
)


def _write(tmp_path: Path, name: str, lines: list[str]) -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n")
    return path


def test_load_golden_set_valid_file(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "a.jsonl",
        [
            '{"id": "ex_1", "cluster_id": "clu_a", "source": "slack", "label": "x", '
            '"content": "hello", "ground_truth": "decision", "ground_truth_reason": "r"}',
            '{"id": "ex_2", "cluster_id": null, "source": "jira", "label": "y", '
            '"content": "world", "ground_truth": "noise", "ground_truth_reason": "r2"}',
        ],
    )

    result = load_golden_set([path])

    assert result.errors == []
    assert len(result.rows) == 2
    assert result.rows[0] == GoldenSetRow(
        id="ex_1",
        cluster_id="clu_a",
        source="slack",
        label="x",
        content="hello",
        ground_truth="decision",
        ground_truth_reason="r",
    )
    assert result.rows[1].cluster_id is None


def test_load_golden_set_skips_blank_lines(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "a.jsonl",
        [
            '{"id": "ex_1", "source": "slack", "label": "x", "content": "hi", '
            '"ground_truth": "signal"}',
            "",
            "   ",
            '{"id": "ex_2", "source": "slack", "label": "x", "content": "hi", '
            '"ground_truth": "noise"}',
        ],
    )

    result = load_golden_set([path])

    assert len(result.rows) == 2
    assert result.errors == []


def test_load_golden_set_reports_invalid_json_without_aborting(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "a.jsonl",
        [
            '{"id": "ex_1", "source": "slack", "label": "x", "content": "hi", '
            '"ground_truth": "signal"}',
            "{not valid json",
            '{"id": "ex_3", "source": "slack", "label": "x", "content": "hi", '
            '"ground_truth": "noise"}',
        ],
    )

    result = load_golden_set([path])

    assert len(result.rows) == 2
    assert [r.id for r in result.rows] == ["ex_1", "ex_3"]
    assert len(result.errors) == 1
    assert result.errors[0].line_number == 2
    assert "invalid JSON" in result.errors[0].reason


def test_load_golden_set_reports_schema_violation_without_aborting(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "a.jsonl",
        [
            # Missing required "content" field.
            '{"id": "ex_1", "source": "slack", "label": "x", "ground_truth": "signal"}',
            # Invalid ground_truth value.
            '{"id": "ex_2", "source": "slack", "label": "x", "content": "hi", '
            '"ground_truth": "maybe"}',
            '{"id": "ex_3", "source": "slack", "label": "x", "content": "hi", '
            '"ground_truth": "noise"}',
        ],
    )

    result = load_golden_set([path])

    assert len(result.rows) == 1
    assert result.rows[0].id == "ex_3"
    assert len(result.errors) == 2
    assert result.errors[0].line_number == 1
    assert result.errors[1].line_number == 2


def test_load_golden_set_concatenates_multiple_files_in_order(tmp_path: Path) -> None:
    path_a = _write(
        tmp_path,
        "a.jsonl",
        [
            '{"id": "ex_1", "source": "slack", "label": "x", "content": "hi", '
            '"ground_truth": "signal"}'
        ],
    )
    path_b = _write(
        tmp_path,
        "b.jsonl",
        [
            '{"id": "ex_2", "source": "slack", "label": "x", "content": "hi", '
            '"ground_truth": "decision"}'
        ],
    )

    result = load_golden_set([path_a, path_b])

    assert [r.id for r in result.rows] == ["ex_1", "ex_2"]


def test_filter_extracted_signal_excludes_noise() -> None:
    rows = [
        GoldenSetRow(id="1", source="s", label="l", content="c", ground_truth="noise"),
        GoldenSetRow(id="2", source="s", label="l", content="c", ground_truth="signal"),
        GoldenSetRow(id="3", source="s", label="l", content="c", ground_truth="decision"),
    ]

    filtered = filter_extracted_signal(rows)

    assert [r.id for r in filtered] == ["2", "3"]

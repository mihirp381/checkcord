"""Golden-set schema and loader.

Rows have id, cluster_id, source, label, content, ground_truth and
ground_truth_reason. Two rows sharing a non-null `cluster_id` describe
the same decision and form a positive pair; rows with a null or
different `cluster_id` do not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError


class GoldenSetRow(BaseModel):
    """One labeled row from a golden-set JSONL file."""

    id: str
    cluster_id: str | None = None
    source: str
    label: str
    content: str
    ground_truth: Literal["noise", "signal", "decision"]
    ground_truth_reason: str = ""


class GoldenSetLoadError(BaseModel):
    """One row that failed to parse or validate; it never aborts the whole load."""

    source_file: str
    line_number: int
    raw_line: str
    reason: str


class GoldenSetLoadResult(BaseModel):
    rows: list[GoldenSetRow]
    errors: list[GoldenSetLoadError]


def load_golden_set(paths: list[Path]) -> GoldenSetLoadResult:
    """Load and validate one or more JSONL golden-set files.

    Files are concatenated in the order given into one corpus. Blank lines
    are skipped. A malformed line (invalid JSON, or JSON that does not satisfy
    `GoldenSetRow`) is recorded in `.errors` with its file, line and reason,
    and the rest of the file still loads.
    """
    rows: list[GoldenSetRow] = []
    errors: list[GoldenSetLoadError] = []

    for path in paths:
        with path.open("r", encoding="utf-8") as f:
            for line_number, raw_line in enumerate(f, start=1):
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError as e:
                    errors.append(
                        GoldenSetLoadError(
                            source_file=str(path),
                            line_number=line_number,
                            raw_line=stripped,
                            reason=f"invalid JSON: {e}",
                        )
                    )
                    continue
                try:
                    rows.append(GoldenSetRow.model_validate(payload))
                except ValidationError as e:
                    errors.append(
                        GoldenSetLoadError(
                            source_file=str(path),
                            line_number=line_number,
                            raw_line=stripped,
                            reason=str(e),
                        )
                    )

    return GoldenSetLoadResult(rows=rows, errors=errors)


def filter_extracted_signal(rows: list[GoldenSetRow]) -> list[GoldenSetRow]:
    """Restrict to rows a real extraction pipeline would hand to ClusteringWorker.

    `ground_truth="noise"` rows (casual chat, bot pings, routine CI noise) are
    filtered out before clustering in production, so scoring against them
    would measure inputs the stage never receives.
    """
    return [row for row in rows if row.ground_truth in ("signal", "decision")]

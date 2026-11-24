"""A GitHub Actions workflow file as jobs and steps."""

from typing import Any

import structlog
import yaml

from checkcord.models.cicd import (
    WorkflowDefinition,
    WorkflowJobDefinition,
    WorkflowStepDefinition,
)

logger = structlog.get_logger(__name__).bind(component="workflow_parser")


def parse_workflow_yaml(workflow_path: str, raw_yaml: bytes) -> WorkflowDefinition | None:
    """Parse one `.github/workflows/*.yml` file's bytes into a `WorkflowDefinition`.

    Returns None (never raises) on malformed YAML or an unexpected top-level
    shape -- a single unparseable workflow file must not abort the caller's
    broader ingestion (same "skip, don't fail the batch" discipline as
    `services/ast_writer.py`).
    """
    try:
        parsed: Any = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as e:
        logger.warning("workflow_parser.invalid_yaml", workflow_path=workflow_path, error=str(e))
        return None

    if not isinstance(parsed, dict):
        logger.warning(
            "workflow_parser.unexpected_top_level_shape",
            workflow_path=workflow_path,
            type=type(parsed).__name__,
        )
        return None

    name = parsed.get("name")
    jobs_raw = parsed.get("jobs")
    jobs: list[WorkflowJobDefinition] = []
    if isinstance(jobs_raw, dict):
        for job_id, job_def in jobs_raw.items():
            if not isinstance(job_def, dict):
                continue
            needs_raw = job_def.get("needs") or []
            needs = [needs_raw] if isinstance(needs_raw, str) else list(needs_raw)

            steps_raw = job_def.get("steps") or []
            steps = [
                WorkflowStepDefinition(
                    name=step.get("name"),
                    uses=step.get("uses"),
                    run=step.get("run"),
                )
                for step in steps_raw
                if isinstance(step, dict)
            ]

            jobs.append(
                WorkflowJobDefinition(
                    job_id=str(job_id),
                    name=job_def.get("name"),
                    needs=needs,
                    steps=steps,
                )
            )

    return WorkflowDefinition(workflow_path=workflow_path, name=name, jobs=jobs)

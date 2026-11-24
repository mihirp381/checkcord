# ruff: noqa: ASYNC101
"""Sends pending meeting extractions to the verifier through the OpenAI Batch API."""

import asyncio
import json
import tempfile
from typing import Any
from uuid import UUID

import structlog
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.bus.interface import EventBus
from checkcord.core import config
from checkcord.llm import usage
from checkcord.llm.factory import build_openai_client
from checkcord.models.common import AccessControl, AuthorInfo
from checkcord.pipeline.agents.extraction_publisher import (
    format_adr_content,
    format_constraint_content,
    format_risk_content,
    publish_extraction_as_context_candidate,
)
from checkcord.pipeline.agents.judge import stored_payload
from checkcord.pipeline.agents.judge_batch import (
    create_judge_batch_request,
    parse_judge_batch_response_line,
)
from checkcord.storage.meeting_extractions import MeetingExtractionStore
from checkcord.storage.postgres import PostgresEventStore, tenant_context

logger = structlog.get_logger(__name__)

# entity_type (from get_pending_extractions) -> canonical_contexts.context_type
_ENTITY_TYPE_TO_CONTEXT_TYPE = {"adr": "adr", "constraint": "constraint", "risk": "risk"}


class JudgeBatchWorker:
    """Sends pending extractions to the verifier through the OpenAI Batch API."""

    def __init__(
        self,
        engine: AsyncEngine,
        openai_client: AsyncOpenAI | None = None,
        batch_limit: int = 100,
        poll_interval_seconds: int = 5,
        bus: EventBus | None = None,
    ) -> None:
        self.engine = engine
        self.client = openai_client or build_openai_client()
        self.batch_limit = batch_limit
        self.poll_interval_seconds = poll_interval_seconds
        self.store = MeetingExtractionStore()
        self.stop_event = asyncio.Event()
        # With a bus, each verified extraction is republished onto the ingest
        # pipeline after its audit status is updated. Without one that step
        # is skipped and auditing still runs.
        self.bus = bus

    async def process_batch_job(
        self, tenant_id: UUID, pending_items: list[dict[str, Any]]
    ) -> tuple[list[UUID], list[UUID]]:
        """Submits a single batch payload to OpenAI Batch API, polls for completion, and returns (verified_ids, rejected_ids)."""  # noqa: E501
        if not pending_items:
            return [], []

        log = logger.bind(tenant_id=str(tenant_id), count=len(pending_items))
        log.info("judge_batch_worker.starting_batch_submission")

        model = config.get_settings().meeting_extraction_llm_model
        # Generate JSONL file
        with tempfile.NamedTemporaryFile("w+", suffix=".jsonl", delete=False) as f:
            for item in pending_items:
                req_obj = create_judge_batch_request(
                    extraction_id=item["extraction_id"],
                    text_content=item["text_content"],
                    payload=stored_payload(
                        item["entity_type"], item["payload"], item.get("text_content") or ""
                    ),
                    model=model,
                )
                f.write(json.dumps(req_obj) + "\n")
            jsonl_filepath = f.name

        with open(jsonl_filepath, "rb") as file_data:  # noqa: ASYNC230
            uploaded_file = await self.client.files.create(
                file=file_data,
                purpose="batch",
            )

        log.info("judge_batch_worker.file_uploaded", file_id=uploaded_file.id)

        batch_job = await self.client.batches.create(
            input_file_id=uploaded_file.id,
            endpoint="/v1/chat/completions",
            completion_window="24h",
        )

        log.info("judge_batch_worker.batch_created", batch_id=batch_job.id)

        # Poll batch status until completed
        while not self.stop_event.is_set():
            batch_status = await self.client.batches.retrieve(batch_job.id)
            status = batch_status.status

            if status == "completed":
                log.info(
                    "judge_batch_worker.batch_completed", output_file_id=batch_status.output_file_id
                )
                break
            elif status in ("failed", "canceled", "expired"):
                log.error(
                    "judge_batch_worker.batch_failed", status=status, errors=batch_status.errors
                )
                return [], [UUID(str(item["extraction_id"])) for item in pending_items]

            await asyncio.sleep(self.poll_interval_seconds)

        # Fetch output JSONL content
        output_file_id = batch_status.output_file_id
        if not output_file_id:
            return [], []

        content_res = await self.client.files.content(output_file_id)
        raw_output_text = content_res.text

        verified_ids: list[UUID] = []
        rejected_ids: list[UUID] = []

        for line in raw_output_text.strip().split("\n"):
            if not line.strip():
                continue
            line_dict = json.loads(line)
            _record_batch_usage(tenant_id, model, line_dict)
            extraction_id, verified = parse_judge_batch_response_line(line_dict)

            if verified.is_supported:
                verified_ids.append(extraction_id)
            else:
                rejected_ids.append(extraction_id)

        log.info(
            "judge_batch_worker.batch_processed",
            verified_count=len(verified_ids),
            rejected_count=len(rejected_ids),
        )

        return verified_ids, rejected_ids

    async def _publish_verified_extractions(
        self,
        tenant_id: UUID,
        pending_items: list[dict[str, Any]],
        verified_ids: list[UUID],
    ) -> None:
        """Republish each verified extraction onto the ingest pipeline with the
        meeting's author, audience and connection, saved on its segment.
        Segments saved before those columns existed fall back to a stored
        meeting event; with neither, the extraction is not published.
        Best effort: a publish failure is logged, never raised; the audit
        status update has already committed by the time this runs.
        """
        if self.bus is None or not verified_ids:
            return

        verified_id_set = set(verified_ids)
        verified_items = [
            item for item in pending_items if item["extraction_id"] in verified_id_set
        ]
        if not verified_items:
            return

        missing = list(
            {item["parent_event_id"] for item in verified_items if item.get("author") is None}
        )
        events_by_id = {}
        if missing:
            async with tenant_context(self.engine, tenant_id, include_held=True) as conn:
                found = await PostgresEventStore(conn).get_events_by_ids(tenant_id, missing)
            events_by_id = {e.event_id: e for e in found}

        for item in verified_items:
            context_type = _ENTITY_TYPE_TO_CONTEXT_TYPE.get(item["entity_type"])
            if context_type is None:
                continue
            parent = events_by_id.get(item["parent_event_id"])
            if item.get("author") is not None and item.get("access_control") is not None:
                author = AuthorInfo.model_validate(item["author"])
                access_control = AccessControl.model_validate(item["access_control"])
            elif parent is not None:
                author, access_control = parent.author, parent.access_control
            else:
                logger.warning(
                    "judge_batch_worker.meeting_audience_missing",
                    extraction_id=str(item["extraction_id"]),
                )
                continue

            payload = item["payload"]
            if item["entity_type"] == "adr":
                content = format_adr_content(
                    payload["final_decision"], payload["context"], item.get("text_content") or ""
                )
            elif item["entity_type"] == "constraint":
                content = format_constraint_content(payload["description"])
            else:
                content = format_risk_content(payload["description"])

            try:
                await publish_extraction_as_context_candidate(
                    self.bus,
                    tenant_id=tenant_id,
                    context_type=context_type,
                    content=content,
                    author=author,
                    access_control=access_control,
                    source_id=f"extraction-{item['entity_type']}-{item['extraction_id']}",
                    connection=item.get("connection"),
                )
            except Exception as e:
                logger.warning(
                    "judge_batch_worker.context_candidate_publish_failed",
                    extraction_id=str(item["extraction_id"]),
                    error=str(e),
                )

    async def run_once(self, tenant_id: UUID | None = None) -> int:
        """Run a single pass: query pending extractions for a tenant, submit batch, update status."""  # noqa: E501
        if tenant_id is None:
            return 0

        async with tenant_context(self.engine, tenant_id) as conn:
            pending_items = await self.store.get_pending_extractions(conn, limit=self.batch_limit)

        if not pending_items:
            return 0

        verified_ids, rejected_ids = await self.process_batch_job(tenant_id, pending_items)
        async with tenant_context(self.engine, tenant_id) as conn:
            await self.store.update_extraction_audit_statuses(conn, verified_ids, rejected_ids)

        await self._publish_verified_extractions(tenant_id, pending_items, verified_ids)

        return len(pending_items)

    def stop(self) -> None:
        self.stop_event.set()


def _record_batch_usage(tenant_id: UUID, model: str, line: dict[str, Any]) -> None:
    body = (line.get("response") or {}).get("body") or {}
    tokens = body.get("usage") or {}
    usage.record(
        usage.CHAT,
        "meeting_judge_batch",
        body.get("model") or model,
        latency_s=0.0,
        failed=bool(line.get("error")),
        input_tokens=tokens.get("prompt_tokens"),
        output_tokens=tokens.get("completion_tokens"),
        tenant_id=tenant_id,
    )


async def run_judge_batch_sweep(db_engine: AsyncEngine, bus: EventBus | None = None) -> int:
    """Run a verifier batch pass for every tenant with pending extractions."""
    if not config.get_settings().openai_api_key:
        logger.debug("judge_batch_worker.sweep_skipped_no_openai_api_key")
        return 0

    store = MeetingExtractionStore()
    tenant_ids = await store.get_tenants_with_pending_extractions(db_engine)

    if not tenant_ids:
        logger.info("judge_batch_worker.sweep_no_pending_tenants")
        return 0

    worker = JudgeBatchWorker(db_engine, bus=bus)
    total_processed = 0
    for tenant_id in tenant_ids:
        try:
            total_processed += await worker.run_once(tenant_id)
        except Exception as e:
            logger.exception(
                "judge_batch_worker.sweep_failed_for_tenant", tenant_id=str(tenant_id), error=str(e)
            )

    logger.info(
        "judge_batch_worker.sweep_completed",
        tenants_processed=len(tenant_ids),
        total_extractions=total_processed,
    )
    return total_processed

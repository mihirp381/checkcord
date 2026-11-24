"""OpenAI Batch API requests for the verifier."""

import json
from typing import Any
from uuid import UUID

import structlog

from checkcord.models.extractions import VerifiedExtraction
from checkcord.pipeline.agents.judge import JUDGE_SYSTEM_PROMPT

logger = structlog.get_logger(__name__)


def create_judge_batch_request(
    extraction_id: UUID | str,
    text_content: str,
    payload: dict[str, Any],
    model: str = "gpt-4o",
) -> dict[str, Any]:
    """Create a single OpenAI Batch API JSONL line dictionary for auditing a candidate extraction."""  # noqa: E501
    payload_str = json.dumps(payload, default=str)
    user_message = (
        f"Verbatim Transcript Segment:\n{text_content}\n\n"
        f"Candidate Extraction Payload:\n{payload_str}"
    )

    request_body = {
        "custom_id": f"judge-audit-{extraction_id}",
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {
            "model": model,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "VerifiedExtraction",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "is_supported": {
                                "type": "boolean",
                                "description": "True if verbatim segment text supports all claims",
                            },
                            "hallucinated_elements": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Unsupported or ungrounded claims",
                            },
                            "rejection_reason": {
                                "type": ["string", "null"],
                                "description": "Reason for rejection if unsupported",
                            },
                        },
                        "required": ["is_supported", "hallucinated_elements", "rejection_reason"],
                        "additionalProperties": False,
                    },
                },
            },
        },
    }
    return request_body


def parse_judge_batch_response_line(line_dict: dict[str, Any]) -> tuple[UUID, VerifiedExtraction]:
    """Parse a single output JSONL line returned by OpenAI Batch API.

    Returns (extraction_id, VerifiedExtraction).
    """
    custom_id = line_dict.get("custom_id", "")
    extraction_id_str = custom_id.replace("judge-audit-", "")
    extraction_id = UUID(extraction_id_str)

    response_body = line_dict.get("response", {}).get("body", {})
    choices = response_body.get("choices", [])
    if not choices:
        return extraction_id, VerifiedExtraction(
            is_supported=False, rejection_reason="No choices in batch response"
        )

    content_str = choices[0].get("message", {}).get("content", "")
    if not content_str:
        return extraction_id, VerifiedExtraction(
            is_supported=False, rejection_reason="Empty content in batch response"
        )

    parsed = json.loads(content_str)
    verified = VerifiedExtraction.model_validate(parsed)
    return extraction_id, verified

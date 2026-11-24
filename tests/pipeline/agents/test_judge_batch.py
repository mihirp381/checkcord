"""Unit tests for Judge Batch API payload construction and response parsing."""

import json
from uuid import uuid4

from checkcord.pipeline.agents.judge_batch import (
    create_judge_batch_request,
    parse_judge_batch_response_line,
)


def test_create_judge_batch_request() -> None:
    extraction_id = uuid4()
    text_content = "Alice: We will use PostgreSQL."
    payload = {"decision_title": "Use PostgreSQL"}

    req = create_judge_batch_request(
        extraction_id=extraction_id,
        text_content=text_content,
        payload=payload,
    )

    assert req["custom_id"] == f"judge-audit-{extraction_id}"
    assert req["method"] == "POST"
    assert req["url"] == "/v1/chat/completions"
    assert req["body"]["model"] == "gpt-4o"
    assert req["body"]["response_format"]["type"] == "json_schema"


def test_parse_judge_batch_response_line() -> None:
    extraction_id = uuid4()
    sample_response_line = {
        "custom_id": f"judge-audit-{extraction_id}",
        "response": {
            "body": {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "is_supported": True,
                                    "hallucinated_elements": [],
                                    "rejection_reason": None,
                                }
                            )
                        }
                    }
                ]
            }
        },
    }

    parsed_id, verified = parse_judge_batch_response_line(sample_response_line)

    assert parsed_id == extraction_id
    assert verified.is_supported is True
    assert verified.hallucinated_elements == []
    assert verified.rejection_reason is None

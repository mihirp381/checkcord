from uuid import uuid4

from checkcord.adapters.linear_adapter import LinearAdapter
from checkcord.adapters.tasks.providers.linear import router as linear


def test_linear_comments_are_their_own_events_under_their_authors() -> None:
    issue = {
        "id": "id-1",
        "identifier": "ENG-1",
        "title": "Retries",
        "description": "Retry webhooks with backoff.",
        "creator": {"name": "Tomas", "email": "tomas@x.test"},
        "comments": {
            "nodes": [
                {
                    "id": "c1",
                    "body": "Not before idempotency keys.",
                    "user": {"name": "Lena", "email": "lena@x.test"},
                    "createdAt": "2026-03-02T10:00:00.000Z",
                }
            ]
        },
    }
    ticket, comment = linear.issue_events(LinearAdapter(api_key="k"), uuid4(), issue, "k", "ENG")
    assert ticket.author.email == "tomas@x.test"
    assert "idempotency" not in ticket.content_clean
    assert "idempotency" not in str(ticket.source_raw_payload)
    assert comment.author.email == "lena@x.test"
    assert comment.source_id == "ENG-1:comment:c1"
    assert "idempotency" in comment.content_clean

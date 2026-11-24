from uuid import uuid4

import pytest
from pydantic import ValidationError

from checkcord.connectors.signed_webhook import routes as router
from checkcord.connectors.signed_webhook.payload import SignedWebhookEvent, to_event
from checkcord.models.enums import AccessVisibility, SourcePlatform

TENANT = uuid4()


def test_signature_covers_timestamp_and_body() -> None:
    secret = "s-" + uuid4().hex
    sig = router.signature(secret, "1700000000", b'{"id": "1"}')
    assert sig.startswith("sha256=") and len(sig) == 7 + 64
    assert sig != router.signature(secret, "1700000001", b'{"id": "1"}')
    assert sig != router.signature(secret, "1700000000", b'{"id": "2"}')
    assert sig != router.signature(secret + "x", "1700000000", b'{"id": "1"}')


@pytest.mark.parametrize(
    ("timestamp", "fresh"),
    [("1000", True), ("1300", True), ("700", True), ("1301", False), ("699", False)]
    + [(None, False), ("", False), ("soon", False)],
)
def test_timestamp_must_be_within_five_minutes(timestamp: str | None, fresh: bool) -> None:
    assert router._fresh(timestamp, 1000.0) is fresh


def test_private_by_default_with_resolved_readers() -> None:
    reader = uuid4()
    item = SignedWebhookEvent(id="inc-7", text="Checkout is down", container="ops")
    event = to_event(item, TENANT, "webhook:status-page", [reader])
    assert event.source_platform == SourcePlatform.CUSTOM
    assert event.connection == "webhook:status-page"
    assert event.access_control.is_public is False
    assert event.access_control.visibility == AccessVisibility.PRIVATE
    assert event.access_control.allowed_identity_ids == [reader]
    assert event.access_control.source_group_id == "ops"
    assert "text" not in event.source_raw_payload


def test_public_audience_names_nobody() -> None:
    item = SignedWebhookEvent.model_validate(
        {"id": "a", "text": "t", "title": "T", "audience": {"public": True}}
    )
    event = to_event(item, TENANT, "webhook:x", [uuid4()])
    assert event.access_control.is_public is True
    assert event.access_control.allowed_identity_ids == []
    assert event.document_metadata is not None and event.document_metadata.title == "T"


@pytest.mark.parametrize("body", [{}, {"id": "", "text": "t"}, {"id": "a", "text": ""}])
def test_id_and_text_are_required(body: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        SignedWebhookEvent.model_validate(body)

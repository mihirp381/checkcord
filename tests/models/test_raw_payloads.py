from datetime import UTC, datetime
from uuid import uuid4

from checkcord.models.enums import SourcePlatform
from checkcord.models.raw_payloads import RawChatPayload


def test_raw_chat_payload_redis_serialization() -> None:
    tenant_id = uuid4()
    now = datetime.now(UTC)
    payload = RawChatPayload(
        tenant_id=tenant_id,
        platform=SourcePlatform.SLACK,
        channel_id="C123456",
        channel_name="general",
        author_id="U654321",
        author_name="Alice",
        message_id="1700000000.000100",
        thread_ts="1700000000.000050",
        happened_at=now,
        content_raw="Hello world!",
        content_clean="Hello world!",
        access_control={"is_public": True},
        raw_event={"type": "message", "text": "Hello world!"},
    )

    # Serialize to redis dict
    redis_dict = payload.to_redis_dict()

    assert redis_dict["tenant_id"] == str(tenant_id)
    assert redis_dict["platform"] == "slack"
    assert redis_dict["channel_id"] == "C123456"
    assert redis_dict["group_id"] == "__NONE__"

    # Deserialize back from redis dict
    reconstructed = RawChatPayload.from_redis_dict(redis_dict)

    assert reconstructed.payload_id == payload.payload_id
    assert reconstructed.tenant_id == payload.tenant_id
    assert reconstructed.platform == payload.platform
    assert reconstructed.channel_id == payload.channel_id
    assert reconstructed.group_id is None
    assert reconstructed.author_id == "U654321"
    assert reconstructed.message_id == "1700000000.000100"
    assert reconstructed.thread_ts == "1700000000.000050"
    assert reconstructed.content_raw == "Hello world!"
    assert reconstructed.raw_event == {"type": "message", "text": "Hello world!"}

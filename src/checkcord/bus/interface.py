from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from checkcord.models.event import CanonicalEvent

# Type alias for event handlers. Not all topics carry a CanonicalEvent payload
# (see payload_type on subscribe()), so the handler argument is typed loosely.
EventHandler = Callable[[Any], Awaitable[None]]


class EventBus(Protocol):
    async def publish(self, topic: str, event: CanonicalEvent) -> str:
        """Publish an event to a topic. Returns the message ID."""
        ...

    async def subscribe(
        self,
        topic: str,
        group: str,
        consumer_name: str,
        handler: EventHandler,
        payload_type: type[Any] = CanonicalEvent,
    ) -> None:
        """Subscribe to a topic using a consumer group and process events using a handler.

        payload_type controls how each message body is deserialized before being
        passed to handler. Defaults to CanonicalEvent, the payload type used by
        every topic except events.raw.chat and events.scrubbed.chat (RawChatPayload).
        """
        ...

    async def acknowledge(self, topic: str, group: str, msg_id: str) -> None:
        """Acknowledge a message in the consumer group."""
        ...

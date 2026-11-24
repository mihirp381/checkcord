from pydantic import BaseModel


class DatadogWebhookPayload(BaseModel):
    id: str | int
    title: str
    body: str
    event_type: str | None = None
    url: str | None = None
    date: int | None = None  # Unix timestamp

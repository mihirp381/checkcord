from pydantic import BaseModel, Field


class SentryFrame(BaseModel):
    filename: str | None = None
    function: str | None = None
    lineNo: int | None = Field(default=None, alias="lineno")
    in_app: bool | None = None


class SentryStacktrace(BaseModel):
    frames: list[SentryFrame] = Field(default_factory=list)


class SentryExceptionValue(BaseModel):
    type: str | None = None
    value: str | None = None
    stacktrace: SentryStacktrace | None = None


class SentryException(BaseModel):
    values: list[SentryExceptionValue] = Field(default_factory=list)


class SentryEvent(BaseModel):
    event_id: str
    title: str | None = None
    message: str | None = None
    culprit: str | None = None
    timestamp: float | None = None
    exception: SentryException | None = None


class SentryWebhookPayload(BaseModel):
    id: str | None = None
    project: str | None = None
    project_name: str | None = Field(default=None, alias="project_name")
    project_slug: str | None = Field(default=None, alias="project_slug")
    level: str | None = None
    message: str | None = None
    url: str | None = None
    event: SentryEvent | None = None

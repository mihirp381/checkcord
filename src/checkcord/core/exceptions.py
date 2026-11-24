"""Checkcord's exceptions."""


class CheckcordError(Exception):
    """Base exception for all Checkcord errors."""

    pass


class IngestionError(CheckcordError):
    pass


class ASTParsingError(CheckcordError):
    pass


class ASTHashingError(CheckcordError):
    pass


class LinkageAdjudicationError(CheckcordError):
    """Raised when record-linkage adjudication can't reach a verdict on a
    candidate context -- e.g. an LLM timeout or exception in
    `LinkageWorker.process_message`.

    Callers must let this propagate so the event bus's existing retry/DLQ
    machinery (PEL redelivery, then DLQ after max_retries) handles it.
    Falling through to bootstrap a new canonical context on adjudication
    failure silently mints a duplicate that pollutes all future clustering
    against it -- fail-open is the wrong direction here.
    """

    pass


class GitHubAPIError(CheckcordError):
    """Raised by GitHubRestClient methods on unrecoverable failure.

    Covers exhausted rate-limit backoff and non-2xx responses that are not a
    legitimate "no data" result. Only the backfill and CI methods raise it;
    the older methods return False, None or []. Callers must let it propagate
    rather than treat a failed call as an empty result.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)

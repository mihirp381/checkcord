"""Typed wire payload for the repo.backfill.requested stream.

Published by `api/repo_router.py` and consumed by
`workers/repo_backfill.py::RepoBackfillWorker` through `EventBus.subscribe()`.
"""

from uuid import UUID

from pydantic import BaseModel


class RepoBackfillRequestPayload(BaseModel):
    """The full repo.backfill.requested message.

    Deliberately carries only identifiers, not the PAT -- the backfill
    worker looks up the current `GithubInstallation` row by
    `repo_identifier` at processing time, so a rotated/updated token is
    always used even if this message sits in the stream (PEL/DLQ retry)
    for a while before being processed.
    """

    tenant_id: UUID
    repo_identifier: str

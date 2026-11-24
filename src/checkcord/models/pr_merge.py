"""Typed wire payload for the ci.pr.merged stream.

Published by `adapters/code/providers/github/webhook.py` and consumed through
`EventBus.subscribe()`.

No GitHub token is carried: the worker looks it up from `github_installations`.
"""

from uuid import UUID

from pydantic import BaseModel


class PRMergedPayload(BaseModel):
    """Triggers workers/ast_writer_worker.py to write AST nodes for a merged PR's changed files.

    Fired only for `pull_request` `closed` with `merged=true`.
    """

    tenant_id: UUID
    repository_full_name: str
    pull_request_number: int
    merge_commit_sha: str

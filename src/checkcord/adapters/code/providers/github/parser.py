from uuid import UUID, uuid4

import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.chat.core.identity import resolve_author_identity
from checkcord.adapters.code.providers.github.rest_client import GitHubRestClient
from checkcord.adapters.code.providers.github.schemas import (
    GitHubIssueCommentPayload,
    GitHubPRPayload,
    GitHubPRReviewCommentPayload,
    GitHubPRReviewPayload,
    GitHubPushPayload,
)
from checkcord.models.common import AccessControl, AuthorInfo, CodeMetadata
from checkcord.models.enums import EventAction, SourcePlatform, SourceType
from checkcord.models.event import CanonicalEvent
from checkcord.services import accounts, audiences
from checkcord.storage.models import EnterpriseUser
from checkcord.storage.postgres import tenant_context

logger = structlog.get_logger(__name__).bind(component="github_parser")

OWN_BRANCH_PREFIX = "checkcord/"


def own_branch(ref: str | None) -> bool:
    return (ref or "").removeprefix("refs/heads/").startswith(OWN_BRANCH_PREFIX)


async def resolve_github_user(
    db_engine: AsyncEngine,
    tenant_id: UUID,
    username: str | None,
    email: str | None,
) -> tuple[UUID | None, str | None, str | None]:
    """Resolve a GitHub user's username/email to an enterprise user ID."""
    # Try resolving by username using ChatUser mapping
    if username:
        internal_id, resolved_email, display_name = await resolve_author_identity(
            db_engine, tenant_id, "github", username
        )
        if internal_id:
            return internal_id, resolved_email or email, display_name

    # Try resolving by email against EnterpriseUser table directly
    if email:
        async with tenant_context(db_engine, tenant_id) as conn:
            stmt = select(EnterpriseUser).where(
                EnterpriseUser.tenant_id == tenant_id,
                EnterpriseUser.primary_email == email,
            )
            res = await conn.execute(stmt)
            ent_user = res.mappings().fetchone()
            if ent_user:
                return ent_user["id"], email, ent_user["display_name"]

    return None, email, username or email


async def read_github_repository(
    client: GitHubRestClient, repo_name: str, private: bool | None = None
) -> audiences.Reading:
    """Visibility and collaborator logins; raises ``ReadFailed`` when either is unreadable."""
    owner, repo = repo_name.split("/", 1)
    if private is None:
        details = await client.get_repository(owner, repo)
        if not details:
            raise audiences.ReadFailed("repository unavailable")
        # Internal repositories report private=true and are treated as private.
        private = details.get("private") is not False
    if not private:
        return audiences.Reading(is_public=True)
    logins = await client.get_collaborators(owner, repo)
    if not logins:
        raise audiences.ReadFailed("collaborators unavailable")
    return audiences.Reading(
        is_public=False, native_ids=tuple(logins), lookup=lambda ids: lookup_logins(client, ids)
    )


async def lookup_logins(client: GitHubRestClient, logins: list[str]) -> list[accounts.Profile]:
    """Public profiles of logins; only the email a user made public is used, never commit
    emails."""
    found = []
    for login in logins:
        user = await client.get_user(login)
        if user is not None:
            found.append(
                accounts.Profile(
                    native_id=login,
                    display_name=user.get("name") or login,
                    email=user.get("email") or None,
                )
            )
    return found


async def get_repository_acl(
    db_engine: AsyncEngine,
    redis_client: Redis | None,
    tenant_id: UUID,
    repo_name: str,
    private: bool,
    pat: str,
) -> AccessControl:
    """The repository's stored audience, read again when stale or contradicted by ``private``."""
    client = GitHubRestClient(pat)
    return await audiences.current(
        db_engine,
        tenant_id,
        audiences.GITHUB,
        repo_name,
        reader=lambda: read_github_repository(client, repo_name, private),
        is_public_hint=not private,
        redis=redis_client,
    )


async def parse_pull_request_event(
    payload: GitHubPRPayload,
    db_engine: AsyncEngine,
    redis_client: Redis | None,
    tenant_id: UUID,
    pat: str,
) -> CanonicalEvent | None:
    """Parse GitHub pull_request webhook payload into CanonicalEvent.

    Handles "opened" and "closed" with `pull_request.merged` true; every
    other action returns None. The merged case fetches files at the merge
    commit SHA, not the PR head SHA (squash and rebase merges can differ),
    and sets `CodeMetadata.merge_commit=True`.
    """
    is_merge = payload.action == "closed" and payload.pull_request.merged
    if payload.action != "opened" and not is_merge:
        return None

    pr = payload.pull_request
    if own_branch(pr.head.ref):
        return None
    owner, repo = payload.repository.full_name.split("/")

    # For a merge, prefer the merge commit SHA over the PR head SHA; fall
    # back to the head SHA if GitHub omitted merge_commit_sha.
    ref_for_files = (pr.merge_commit_sha or pr.head.sha) if is_merge else pr.head.sha

    client = GitHubRestClient(pat)
    files_changed, additions, deletions = await client.get_pull_request_files(
        owner, repo, pr.number
    )

    internal_id, email, display_name = await resolve_github_user(
        db_engine, tenant_id, pr.user.login, pr.user.email
    )
    author = AuthorInfo(
        source_id=pr.user.login,
        internal_id=internal_id,
        email=email,
        display_name=display_name,
    )

    acl = await get_repository_acl(
        db_engine,
        redis_client,
        tenant_id,
        payload.repository.full_name,
        payload.repository.private,
        pat,
    )

    code_meta = CodeMetadata(
        repository=payload.repository.full_name,
        branch=pr.base.ref if is_merge else pr.head.ref,
        files_changed=files_changed[:100],
        lines_added=additions,
        lines_removed=deletions,
        pull_request_id=str(pr.number),
        commit_sha=ref_for_files,
        merge_commit=is_merge,
    )

    if is_merge:
        content = f"Pull Request #{pr.number} Merged: {pr.title}\n\n{pr.body or ''}"
        happened_at = pr.merged_at or pr.created_at
        source_id = f"pr:{payload.repository.full_name}:{pr.number}:merged"
    else:
        content = f"Pull Request #{pr.number} Opened: {pr.title}\n\n{pr.body or ''}"
        happened_at = pr.created_at
        source_id = f"pr:{payload.repository.full_name}:{pr.number}"

    return CanonicalEvent(
        event_id=uuid4(),
        tenant_id=tenant_id,
        source_platform=SourcePlatform.GITHUB,
        source_type=SourceType.PR_OPENED,
        event_action=EventAction.CREATED,
        source_id=source_id,
        version=1,
        happened_at=happened_at,
        author=author,
        content_raw=content,
        content_clean=content,
        source_raw_payload=payload.model_dump(mode="json"),
        access_control=acl,
        audience_container=payload.repository.full_name,
        code_metadata=code_meta,
        metadata={"html_url": pr.html_url},
    )


async def parse_pr_review_event(
    payload: GitHubPRReviewPayload,
    db_engine: AsyncEngine,
    redis_client: Redis | None,
    tenant_id: UUID,
    pat: str,
) -> CanonicalEvent | None:
    """Parse GitHub pull_request_review webhook payload into CanonicalEvent."""
    if payload.action != "submitted":
        return None

    review = payload.review
    pr = payload.pull_request
    if own_branch(pr.head.ref):
        return None

    internal_id, email, display_name = await resolve_github_user(
        db_engine, tenant_id, review.user.login, review.user.email
    )
    author = AuthorInfo(
        source_id=review.user.login,
        internal_id=internal_id,
        email=email,
        display_name=display_name,
    )

    acl = await get_repository_acl(
        db_engine,
        redis_client,
        tenant_id,
        payload.repository.full_name,
        payload.repository.private,
        pat,
    )

    code_meta = CodeMetadata(
        repository=payload.repository.full_name,
        branch=pr.head.ref,
        files_changed=[],
        lines_added=0,
        lines_removed=0,
        pull_request_id=str(pr.number),
        commit_sha=pr.head.sha,
        merge_commit=False,
    )

    content = f"PR #{pr.number} Review Submitted ({review.state}): {review.body or ''}"

    return CanonicalEvent(
        event_id=uuid4(),
        tenant_id=tenant_id,
        source_platform=SourcePlatform.GITHUB,
        source_type=SourceType.PR_REVIEW,
        event_action=EventAction.CREATED,
        source_id=f"pr_review:{payload.repository.full_name}:{review.id}",
        version=1,
        happened_at=review.submitted_at,
        author=author,
        content_raw=content,
        content_clean=content,
        source_raw_payload=payload.model_dump(mode="json"),
        access_control=acl,
        audience_container=payload.repository.full_name,
        code_metadata=code_meta,
        metadata={"html_url": pr.html_url},
    )


async def parse_pr_review_comment_event(
    payload: GitHubPRReviewCommentPayload,
    db_engine: AsyncEngine,
    redis_client: Redis | None,
    tenant_id: UUID,
    pat: str,
) -> CanonicalEvent | None:
    """Parse GitHub pull_request_review_comment webhook payload into CanonicalEvent."""
    if payload.action != "created":
        return None

    comment = payload.comment
    pr = payload.pull_request
    if own_branch(pr.head.ref):
        return None

    internal_id, email, display_name = await resolve_github_user(
        db_engine, tenant_id, comment.user.login, comment.user.email
    )
    author = AuthorInfo(
        source_id=comment.user.login,
        internal_id=internal_id,
        email=email,
        display_name=display_name,
    )

    acl = await get_repository_acl(
        db_engine,
        redis_client,
        tenant_id,
        payload.repository.full_name,
        payload.repository.private,
        pat,
    )

    code_meta = CodeMetadata(
        repository=payload.repository.full_name,
        branch=pr.head.ref,
        files_changed=[],
        lines_added=0,
        lines_removed=0,
        pull_request_id=str(pr.number),
        commit_sha=pr.head.sha,
        merge_commit=False,
    )

    content = f"PR #{pr.number} Comment Created: {comment.body}"

    return CanonicalEvent(
        event_id=uuid4(),
        tenant_id=tenant_id,
        source_platform=SourcePlatform.GITHUB,
        source_type=SourceType.PR_COMMENT,
        event_action=EventAction.CREATED,
        source_id=f"pr_comment:{payload.repository.full_name}:{comment.id}",
        version=1,
        happened_at=comment.created_at,
        author=author,
        content_raw=content,
        content_clean=content,
        source_raw_payload=payload.model_dump(mode="json"),
        access_control=acl,
        audience_container=payload.repository.full_name,
        code_metadata=code_meta,
        metadata={"html_url": pr.html_url},
    )


async def parse_issue_comment_event(
    payload: GitHubIssueCommentPayload,
    db_engine: AsyncEngine,
    redis_client: Redis | None,
    tenant_id: UUID,
    pat: str,
) -> CanonicalEvent | None:
    """Parse GitHub issue_comment payload into CanonicalEvent (PR comments only)."""
    if payload.action != "created" or "pull_request" not in payload.issue:
        return None

    comment = payload.comment
    issue = payload.issue
    pr_url = issue["pull_request"].get("html_url", "")
    pr_number = issue.get("number", 0)
    owner, repo = payload.repository.full_name.split("/")
    pull = await GitHubRestClient(pat).get_pull_request(owner, repo, pr_number)
    if pull and own_branch((pull.get("head") or {}).get("ref")):
        return None

    internal_id, email, display_name = await resolve_github_user(
        db_engine, tenant_id, comment.user.login, comment.user.email
    )
    author = AuthorInfo(
        source_id=comment.user.login,
        internal_id=internal_id,
        email=email,
        display_name=display_name,
    )

    acl = await get_repository_acl(
        db_engine,
        redis_client,
        tenant_id,
        payload.repository.full_name,
        payload.repository.private,
        pat,
    )

    code_meta = CodeMetadata(
        repository=payload.repository.full_name,
        branch="main",  # Fallback target branch
        files_changed=[],
        lines_added=0,
        lines_removed=0,
        pull_request_id=str(pr_number),
        commit_sha=None,
        merge_commit=False,
    )

    content = f"PR #{pr_number} Comment Created: {comment.body}"

    return CanonicalEvent(
        event_id=uuid4(),
        tenant_id=tenant_id,
        source_platform=SourcePlatform.GITHUB,
        source_type=SourceType.PR_COMMENT,
        event_action=EventAction.CREATED,
        source_id=f"issue_comment:{payload.repository.full_name}:{comment.id}",
        version=1,
        happened_at=comment.created_at,
        author=author,
        content_raw=content,
        content_clean=content,
        source_raw_payload=payload.model_dump(mode="json"),
        access_control=acl,
        audience_container=payload.repository.full_name,
        code_metadata=code_meta,
        metadata={"html_url": pr_url},
    )


async def parse_push_event(
    payload: GitHubPushPayload,
    db_engine: AsyncEngine,
    redis_client: Redis | None,
    tenant_id: UUID,
    pat: str,
) -> list[CanonicalEvent]:
    """Parse GitHub push event into a list of CanonicalEvents (one per commit)."""
    if own_branch(payload.ref):
        return []
    events = []
    branch = payload.ref.removeprefix("refs/heads/")

    owner, repo = payload.repository.full_name.split("/")
    client = GitHubRestClient(pat)

    acl = await get_repository_acl(
        db_engine,
        redis_client,
        tenant_id,
        payload.repository.full_name,
        payload.repository.private,
        pat,
    )

    for commit in payload.commits:
        # Fetch file additions and deletions details for the commit
        files_changed, additions, deletions = await client.get_commit_details(
            owner, repo, commit.id
        )
        if not files_changed:
            # Fallback to files listed in the commit payload
            files_changed = list(set(commit.added + commit.removed + commit.modified))[:100]
        else:
            # Enforce 100-file cap regardless of the source to prevent payload bloat
            files_changed = files_changed[:100]

        internal_id, email, display_name = await resolve_github_user(
            db_engine, tenant_id, commit.author.username, commit.author.email
        )
        author = AuthorInfo(
            source_id=commit.author.username or commit.author.email,
            internal_id=internal_id,
            email=email,
            display_name=display_name or commit.author.name,
        )

        code_meta = CodeMetadata(
            repository=payload.repository.full_name,
            branch=branch,
            files_changed=files_changed,
            lines_added=additions,
            lines_removed=deletions,
            pull_request_id=None,
            commit_sha=commit.id,
            merge_commit=False,
        )

        events.append(
            CanonicalEvent(
                event_id=uuid4(),
                tenant_id=tenant_id,
                source_platform=SourcePlatform.GITHUB,
                source_type=SourceType.COMMIT,
                event_action=EventAction.CREATED,
                source_id=f"commit:{payload.repository.full_name}:{commit.id}",
                version=1,
                happened_at=commit.timestamp,
                author=author,
                content_raw=commit.message,
                content_clean=commit.message,
                source_raw_payload=payload.model_dump(mode="json"),
                access_control=acl,
                audience_container=payload.repository.full_name,
                code_metadata=code_meta,
            )
        )

    return events

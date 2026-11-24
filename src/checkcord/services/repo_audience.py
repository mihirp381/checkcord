"""Who can read a GitHub repository, and which records may be written into it."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.code.providers.github.rest_client import GitHubRestClient
from checkcord.services import accounts

logger = structlog.get_logger(__name__).bind(component="repo_audience")


@dataclass(frozen=True)
class RepoAudience:
    """Checkcord identities that can read a repository; ``None`` when not fully known."""

    identity_ids: frozenset[str] | None = None

    def allows(self, access_control: Mapping[str, Any] | None) -> bool:
        """Whether every reader of the repository may see the record under row-level security."""
        ac = access_control or {}
        if ac.get("is_public") is True:
            return True
        if not self.identity_ids:
            return False
        allowed = {
            str(uid)
            for key in ("allowed_identity_ids", "admin_overrides")
            for uid in ac.get(key) or []
        }
        return self.identity_ids <= allowed


async def resolve_repo_audience(
    db_engine: AsyncEngine | None,
    tenant_id: UUID,
    repo_full_name: str,
    client: GitHubRestClient,
) -> RepoAudience:
    log = logger.bind(repo=repo_full_name, tenant_id=str(tenant_id))
    if db_engine is None or "/" not in repo_full_name:
        return RepoAudience()
    try:
        owner, repo = repo_full_name.split("/", 1)
        details = await client.get_repository(owner, repo)
        # Public and internal repositories are readable beyond their collaborator list.
        if (
            not details
            or details.get("private") is not True
            or details.get("visibility", "private") != "private"
        ):
            log.info("repo_audience.not_private_only_public_records")
            return RepoAudience()

        # Read fresh: a cached list would miss a collaborator added since.
        logins = await client.get_collaborators(owner, repo)
        if not logins:
            log.warning("repo_audience.collaborators_unavailable")
            return RepoAudience()

        from checkcord.adapters.code.providers.github.parser import lookup_logins

        mapped, unmapped = await accounts.match(
            db_engine, tenant_id, "github", logins, lambda ids: lookup_logins(client, ids)
        )
        if unmapped:
            log.warning("repo_audience.unmapped_collaborators", count=len(unmapped))
            return RepoAudience()
        return RepoAudience(frozenset(str(uid) for uid in mapped.values()))
    except Exception as e:
        log.warning("repo_audience.lookup_failed", error=str(e))
        return RepoAudience()


async def readable_repositories(
    engine: AsyncEngine, tenant_id: UUID, identity: UUID, repos: Iterable[str]
) -> set[str]:
    """The repositories ``identity`` may read. A repository with no stored GitHub connection
    (indexed through the push API) is read by everyone in the organisation; a connected
    one by its GitHub audience, and by nobody when that cannot be read."""
    from checkcord.adapters.code.providers.github.parser import read_github_repository
    from checkcord.services import audiences
    from checkcord.services.github_tokens import installation_token

    readable = set()
    for repo in repos:
        try:
            token = await installation_token(engine, tenant_id, repo)
            if token is None:
                readable.add(repo)
                continue
            client = GitHubRestClient(token)
            ac = await audiences.current(
                engine,
                tenant_id,
                audiences.GITHUB,
                repo,
                reader=partial(read_github_repository, client, repo),
            )
        except Exception as e:
            logger.warning("repo_audience.readers_unavailable", repo=repo, error=str(e))
            continue
        if ac.is_public or identity in {*ac.allowed_identity_ids, *ac.admin_overrides}:
            readable.add(repo)
    return readable

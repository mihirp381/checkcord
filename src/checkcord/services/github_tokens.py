from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.core.crypto import decrypt_secret
from checkcord.storage.models import GithubInstallation
from checkcord.storage.postgres import tenant_context


async def installation_token(
    engine: AsyncEngine, tenant_id: UUID, repo_identifier: str
) -> str | None:
    async with tenant_context(engine, tenant_id) as conn:
        stored = await conn.scalar(
            select(GithubInstallation.personal_access_token).where(
                GithubInstallation.repo_identifier == repo_identifier,
                GithubInstallation.tenant_id == tenant_id,
            )
        )
    return decrypt_secret(stored) if stored else None


async def github_token_for(
    engine: AsyncEngine | None, tenant_id: UUID, repo_identifier: str
) -> str:
    token = await installation_token(engine, tenant_id, repo_identifier) if engine else None
    if not token:
        raise LookupError(f"no stored GitHub token for {repo_identifier}")
    return token

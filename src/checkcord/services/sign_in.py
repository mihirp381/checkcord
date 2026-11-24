"""Turn validated ID token claims into a person and a session, or a refusal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.core.config import Settings
from checkcord.core.oidc import email_from_claims, groups_from_claims
from checkcord.services import people, sessions
from checkcord.storage import audit
from checkcord.storage.models import EnterpriseUser
from checkcord.storage.postgres import tenant_context


class Refused(Exception):
    def __init__(self, reason: str, message: str, email: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.email = email


@dataclass(frozen=True)
class SignedIn:
    session_id: UUID
    token: str
    user_id: UUID
    email: str
    is_admin: bool


def session_actor(user_id: UUID | None, email: str | None) -> audit.Actor:
    return audit.Actor(
        channel=audit.API, key_name=sessions.ACTOR_KEY_NAME, user_id=user_id, user_email=email
    )


def _display_name(claims: dict[str, Any], email: str) -> str:
    name = claims.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else email


async def complete(
    engine: AsyncEngine,
    tenant_id: UUID,
    claims: dict[str, Any],
    settings: Settings,
    user_agent: str | None = None,
    source: str | None = None,
) -> SignedIn:
    email = email_from_claims(claims, settings.oidc_allow_missing_email_verified)
    if email is None:
        raise Refused(
            "unverified_email",
            "Your identity provider did not confirm your email address, so Checkcord cannot "
            "match you to a person. Ask your administrator.",
            claims.get("email") if isinstance(claims.get("email"), str) else None,
        )
    async with tenant_context(engine, tenant_id) as conn:
        matches = await people.by_email(conn, tenant_id, email)
        if len(matches) > 1:
            raise Refused(
                "ambiguous_person",
                f"More than one person in Checkcord has the email {email}. Ask an admin to fix it.",
                email,
            )
        if matches and not matches[0].active:
            raise Refused(
                "deactivated_person", f"The person {email} is deactivated in Checkcord.", email
            )
        if matches:
            user_id, is_admin = matches[0].id, matches[0].is_admin
            email = matches[0].primary_email
        else:
            domain = email.rsplit("@", 1)[1].lower()
            if domain not in {d.strip().lower() for d in settings.oidc_auto_create_domains}:
                raise Refused(
                    "unknown_person",
                    f"No person with the email {email} is set up in Checkcord. Ask an admin to "
                    "add you, then sign in again.",
                    email,
                )
            user_id, is_admin = uuid4(), False
            await conn.execute(
                EnterpriseUser.__table__.insert().values(
                    id=user_id,
                    tenant_id=tenant_id,
                    primary_email=email,
                    display_name=_display_name(claims, email),
                    active=True,
                )
            )
            await audit.record(
                conn,
                tenant_id,
                session_actor(user_id, email),
                "user.created",
                params={"email": email, "active": True, "source": "sign_in"},
                record_ids=[user_id],
            )
        actor = session_actor(user_id, email)
        if settings.scim_admin_groups:
            from checkcord.scim.groups import sync_admins

            is_admin = user_id in await sync_admins(conn, tenant_id, {user_id}, actor)
        elif settings.oidc_admin_groups_claim:
            groups = groups_from_claims(claims, settings.oidc_admin_groups_claim)
            is_admin = bool(groups & set(settings.oidc_admin_groups))
            await people.apply_admin(conn, tenant_id, user_id, is_admin, actor, "groups_claim")
        session_id, token = await sessions.create(
            conn, tenant_id, user_id, settings, user_agent=user_agent, source=source
        )
        await audit.record(
            conn,
            tenant_id,
            actor,
            "auth.signed_in",
            params={"session_id": session_id, "is_admin": is_admin},
            record_ids=[session_id],
        )
    return SignedIn(session_id, token, user_id, email, is_admin)

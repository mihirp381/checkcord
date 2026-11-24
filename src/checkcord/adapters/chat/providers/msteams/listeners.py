"""Bot Framework messages from Teams, published as raw chat events."""

import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Request, Response, status
from sqlalchemy import select

from checkcord.adapters.chat.core.bot_filter import should_filter_message
from checkcord.adapters.chat.core.identity import resolve_channel_name
from checkcord.adapters.chat.core.normalizer import (
    normalize_chat_text,
)
from checkcord.adapters.chat.providers.msteams.audience import (
    channel_id_of,
    resolve_msteams_audience,
)
from checkcord.adapters.chat.providers.msteams.auth import BotAuthError, verify_bot_token
from checkcord.api import rate_limit
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.models.enums import EventAction, SourcePlatform
from checkcord.models.raw_payloads import RawChatPayload
from checkcord.services import scope
from checkcord.services.connections import connection_key
from checkcord.storage.models import MSTeamsInstallation
from checkcord.storage.postgres import app_bypass_rls_context

logger = structlog.get_logger(__name__)

router = APIRouter()


def _team_of(channel_data: dict[str, Any]) -> str | None:
    """The team's group id, which Graph addresses teams by; ``team.id`` is a thread id."""
    team = channel_data.get("team") or {}
    found = team.get("aadGroupId") or team.get("id") or channel_data.get("teamId")
    return str(found) if found else None


@router.post("/api/messages")
async def handle_msteams_activity(request: Request) -> Response:
    """Handle incoming MS Teams Bot Activity payloads."""
    try:
        activity: dict[str, Any] = await request.json()
    except Exception as e:
        logger.warning("msteams.activity.invalid_json", error=str(e))
        return Response(
            status_code=status.HTTP_400_BAD_REQUEST,
            content="Invalid JSON body",
        )
    if not isinstance(activity, dict):
        return Response(status_code=status.HTTP_400_BAD_REQUEST, content="Invalid JSON body")
    try:
        await verify_bot_token(
            request.headers.get("authorization"),
            activity,
            request.app.state.ctx.settings.msteams_client_id,
        )
    except BotAuthError as err:
        logger.warning("msteams_activity_rejected", reason=str(err))
        return Response(status_code=status.HTTP_401_UNAUTHORIZED, content="Unauthorized")

    activity_type = activity.get("type")
    # Check activity.get("type"). We process types "message", "messageUpdate", and "messageDelete"
    if activity_type not in ("message", "messageUpdate", "messageDelete"):
        logger.debug("msteams.activity.ignored_type", type=activity_type)
        return Response(status_code=status.HTTP_200_OK)

    # Extract ms_tenant_id from activity.get("tenantId") or
    # activity.get("conversation", {}).get("tenantId")
    ms_tenant_id = activity.get("tenantId") or activity.get("conversation", {}).get("tenantId")
    if not ms_tenant_id:
        logger.warning("msteams.activity.no_tenant", activity=activity)
        return Response(
            status_code=status.HTTP_400_BAD_REQUEST,
            content="Missing tenantId",
        )
    await rate_limit.webhook(request, connection_key("teams", ms_tenant_id))

    ctx = request.app.state.ctx
    engine = ctx.db_engine
    redis_client = ctx.redis_client

    if not engine or not redis_client:
        logger.error("msteams.activity.app_context_missing")
        return Response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content="Services not initialized",
        )

    # Query MSTeamsInstallation.tenant_id from the database.
    async with app_bypass_rls_context(engine) as conn:
        stmt = select(MSTeamsInstallation.tenant_id).where(
            MSTeamsInstallation.ms_tenant_id == ms_tenant_id
        )
        res = await conn.execute(stmt)
        row = res.fetchone()
        if not row:
            # If not found, log a warning and discard the event (return 200 OK
            # so Microsoft's server doesn't retry infinitely).
            logger.warning(
                "msteams.activity.no_installation",
                ms_tenant_id=ms_tenant_id,
            )
            return Response(
                status_code=status.HTTP_200_OK,
                content="Tenant not found, event discarded",
            )
        tenant_id: UUID = row[0]

    conversation = activity.get("conversation") or {}
    channel_data = activity.get("channelData") or {}
    if not await scope.allows(
        engine,
        tenant_id,
        connection_key("teams", ms_tenant_id),
        _team_of(channel_data),
        (channel_data.get("team") or {}).get("id"),
        (channel_data.get("channel") or {}).get("id")
        or channel_id_of(conversation.get("id") or ""),
    ):
        return Response(status_code=status.HTTP_200_OK)

    activity_id = activity.get("id")
    if not activity_id:
        logger.warning("msteams.activity.no_id", activity=activity)
        return Response(
            status_code=status.HTTP_400_BAD_REQUEST,
            content="Missing activity ID",
        )

    # Resolve event action, edits, deletes, and content
    is_delete = activity_type == "messageDelete" or activity.get("channelData", {}).get(
        "eventType"
    ) in ("deleteMessage", "messageDelete")
    is_edit = not is_delete and (
        activity_type == "messageUpdate"
        or activity.get("channelData", {}).get("eventType") in ("editMessage", "messageEdit")
        or activity.get("action") == "update"
    )

    if is_delete:
        event_action = EventAction.DELETED
        content_raw = ""
        content_clean = ""
    elif is_edit:
        event_action = EventAction.UPDATED
        # Extract message text from activity.get("text"). For edits/updates,
        # extract text from the update payload.
        content_raw = activity.get("text") or ""
        content_clean, _ = normalize_chat_text(SourcePlatform.MS_TEAMS, content_raw)
    else:
        event_action = EventAction.CREATED
        content_raw = activity.get("text") or ""
        content_clean, _ = normalize_chat_text(SourcePlatform.MS_TEAMS, content_raw)

    # Download text attachments
    if not is_delete:
        attachments = activity.get("attachments") or []
        has_text_attachments = False

        # Check if we actually have text attachments first
        any_text_attachment = False
        for att in attachments:
            content_type = att.get("contentType") or ""
            name = att.get("name") or ""
            _, ext = os.path.splitext(name.lower())
            is_whitelisted_ext = ext in {
                ".txt",
                ".json",
                ".csv",
                ".xml",
                ".yaml",
                ".yml",
                ".md",
                ".py",
                ".js",
                ".ts",
                ".html",
                ".css",
                ".ini",
                ".conf",
                ".sh",
                ".bat",
            }
            if (
                content_type.startswith("text/")
                or content_type == "application/json"
                or is_whitelisted_ext
            ):
                any_text_attachment = True
                break

        if any_text_attachment:
            from checkcord.adapters.chat.providers.msteams.directory_sync import (
                get_msteams_access_token,
            )
            from checkcord.core.config import get_settings

            settings = get_settings()
            token = await get_msteams_access_token(
                settings.msteams_client_id, settings.msteams_client_secret, ms_tenant_id
            )

            for attachment in attachments:
                content_type = attachment.get("contentType") or ""
                name = attachment.get("name") or "Unnamed Attachment"
                _, ext = os.path.splitext(name.lower())
                is_whitelisted_ext = ext in {
                    ".txt",
                    ".json",
                    ".csv",
                    ".xml",
                    ".yaml",
                    ".yml",
                    ".md",
                    ".py",
                    ".js",
                    ".ts",
                    ".html",
                    ".css",
                    ".ini",
                    ".conf",
                    ".sh",
                    ".bat",
                }
                if not (
                    content_type.startswith("text/")
                    or content_type == "application/json"
                    or is_whitelisted_ext
                ):
                    continue

                # Check size limit (5MB)
                metadata_size = (
                    attachment.get("size")
                    or attachment.get("content", {}).get("size")
                    or attachment.get("content", {}).get("fileSize")
                )
                if metadata_size is not None:
                    try:
                        if int(metadata_size) > 5 * 1024 * 1024:
                            logger.warning(
                                "msteams.attachment.too_large",
                                name=name,
                                size=metadata_size,
                            )
                            continue
                    except (ValueError, TypeError):
                        pass

                content_url = attachment.get("contentUrl")
                if not content_url:
                    logger.warning("msteams.attachment.no_content_url", name=name)
                    continue

                if not token:
                    logger.warning("msteams.attachment.no_token", name=name)
                    content_raw += f"\n\n[Warning: Failed to download attachment {name}]"
                    has_text_attachments = True
                    continue

                # Download file
                headers = {"Authorization": f"Bearer {token}"}
                try:
                    import aiohttp

                    async with aiohttp.ClientSession(headers=headers) as session:
                        async with session.get(content_url) as resp:
                            content_length = resp.headers.get("Content-Length")
                            if content_length:
                                try:
                                    if int(content_length) > 5 * 1024 * 1024:
                                        logger.warning(
                                            "msteams.attachment.too_large",
                                            name=name,
                                            size=content_length,
                                        )
                                        continue
                                except (ValueError, TypeError):
                                    pass

                            if resp.status == 200:
                                file_bytes = await resp.read()
                                if len(file_bytes) > 5 * 1024 * 1024:
                                    logger.warning(
                                        "msteams.attachment.too_large",
                                        name=name,
                                        size=len(file_bytes),
                                    )
                                    continue
                                text_content = file_bytes.decode("utf-8", errors="replace")
                                content_raw += f"\n\n=== Attachment: {name} ===\n{text_content}"
                                has_text_attachments = True
                            else:
                                logger.error(
                                    "msteams.attachment.download_failed",
                                    name=name,
                                    status=resp.status,
                                    url=content_url,
                                )
                                content_raw += (
                                    f"\n\n[Warning: Failed to download attachment {name}]"
                                )
                                has_text_attachments = True
                except Exception as e:
                    logger.exception(
                        "msteams.attachment.download_error",
                        name=name,
                        url=content_url,
                        error=str(e),
                    )
                    content_raw += f"\n\n[Warning: Failed to download attachment {name}]"
                    has_text_attachments = True

            if has_text_attachments:
                content_clean, entities = normalize_chat_text(SourcePlatform.MS_TEAMS, content_raw)

    # Resolve Author
    from_data = activity.get("from") or {}
    user_id = from_data.get("aadObjectId") or from_data.get("id") or "unknown"
    role = from_data.get("role")
    bot_id = user_id if role == "bot" else None

    internal_id, email, display_name = None, None, None
    if user_id and user_id != "unknown":
        from checkcord.adapters.chat.core.identity import resolve_author_identity

        internal_id, email, display_name = await resolve_author_identity(
            db_engine=engine,
            tenant_id=tenant_id,
            platform=SourcePlatform.MS_TEAMS.value,
            native_id=user_id,
        )

    if not display_name:
        display_name = from_data.get("name")

    conversation_type = conversation.get("conversationType") or ""
    conversation_id = conversation.get("id") or "unknown"
    channel_id = (channel_data.get("channel") or {}).get("id") or channel_id_of(conversation_id)
    access_control = await resolve_msteams_audience(
        redis=redis_client,
        db_engine=engine,
        tenant_id=tenant_id,
        ms_tenant_id=ms_tenant_id,
        conversation_type=conversation_type,
        channel_id=channel_id,
        team_id=_team_of(channel_data),
    )
    channel_name = (channel_data.get("channel") or {}).get("name") or await resolve_channel_name(
        engine, tenant_id, SourcePlatform.MS_TEAMS.value, channel_id
    )

    # Resolve timestamp: timezone-aware (UTC)
    timestamp_str = activity.get("timestamp")
    happened_at: datetime
    if timestamp_str:
        try:
            # Python 3.11+ supports 'Z' natively. For safety, replace Z with +00:00.
            cleaned_ts = timestamp_str
            if cleaned_ts.endswith("Z"):
                cleaned_ts = cleaned_ts[:-1] + "+00:00"
            happened_at = datetime.fromisoformat(cleaned_ts)
        except Exception:
            happened_at = datetime.now(UTC)
    else:
        happened_at = datetime.now(UTC)

    # Ensure happened_at is timezone-aware
    if happened_at.tzinfo is None:
        happened_at = happened_at.replace(tzinfo=UTC)

    raw_payload = RawChatPayload(
        tenant_id=tenant_id,
        platform=SourcePlatform.MS_TEAMS,
        channel_id=channel_id,
        channel_name=channel_name,
        author_id=user_id,
        author_email=email,
        author_name=display_name,
        message_id=activity_id,
        thread_ts=activity.get("replyToId"),
        happened_at=happened_at,
        content_raw=content_raw,
        content_clean=content_clean,
        raw_event=activity,
        access_control=access_control,
        connection=connection_key("teams", ms_tenant_id),
    )

    # Check bot noise filtering before publishing. Skip filtering for deleted messages.
    if event_action != EventAction.DELETED:
        if should_filter_message(
            platform=SourcePlatform.MS_TEAMS,
            bot_id=bot_id,
            username=display_name,
            text=content_raw,
        ):
            logger.info(
                "msteams.activity.bot_filtered",
                message_id=activity_id,
                bot_id=bot_id,
                username=display_name,
            )
            return Response(status_code=status.HTTP_200_OK)

    # Publish to events.raw.chat
    event_bus = RedisStreamsEventBus(redis_client)
    try:
        msg_id = await event_bus.publish(topic="events.raw.chat", event=raw_payload)
        logger.info(
            "msteams.activity.published",
            payload_id=str(raw_payload.payload_id),
            msg_id=msg_id,
            action=event_action.value,
        )
    except Exception as e:
        logger.error(
            "msteams.activity.publish_failed",
            error=str(e),
            payload_id=str(raw_payload.payload_id),
        )
        return Response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content="Failed to publish event",
        )

    return Response(status_code=status.HTTP_200_OK)

"""Slack message events, normalized and published as raw chat events."""

import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import aiohttp
import structlog
from slack_bolt.app.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from checkcord.adapters.chat.core.bot_filter import should_filter_message
from checkcord.adapters.chat.core.identity import resolve_channel_name
from checkcord.adapters.chat.core.normalizer import normalize_slack_text
from checkcord.adapters.chat.providers.slack.audience import (
    read_slack_channel,
    resolve_slack_audience,
)
from checkcord.adapters.chat.providers.slack.oauth import PostgresInstallationStore
from checkcord.bus.redis_streams import RedisStreamsEventBus
from checkcord.core.config import get_settings
from checkcord.core.crypto import decrypt_secret
from checkcord.models.enums import EventAction, SourcePlatform
from checkcord.models.raw_payloads import RawChatPayload
from checkcord.services import audiences, scope
from checkcord.services.connections import connection_key
from checkcord.storage.models import SlackInstallation
from checkcord.storage.postgres import (
    app_bypass_rls_context,
)

logger = structlog.get_logger(__name__)


def extract_text_from_blocks(blocks: list[dict[str, Any]]) -> str:
    """Recursively extract raw text from Slack blocks handling text, mrkdwn, plain_text."""
    if not blocks:
        return ""

    texts: list[str] = []

    def _recurse(item: Any) -> None:
        if isinstance(item, list):
            for element in item:
                _recurse(element)
        elif isinstance(item, dict):
            val = item.get("text")
            if isinstance(val, str):
                texts.append(val)
            elif isinstance(val, dict):
                _recurse(val)
            else:
                for k, v in item.items():
                    if k != "type":
                        _recurse(v)

    _recurse(blocks)
    return "".join(texts)


def is_generic_or_empty(text: str) -> bool:
    """Check if Slack message text is empty or generic fallback text."""
    if not text:
        return True
    text_stripped = text.strip()
    if not text_stripped:
        return True

    # Generic Slack fallbacks or placeholder text
    generic_patterns = {
        "this content is not supported",
        "this message has no text",
        "message with blocks",
    }
    return text_stripped.lower() in generic_patterns


def register_listeners(
    app: AsyncApp,
    bus: RedisStreamsEventBus,
    db_engine: AsyncEngine,
) -> None:
    """Register Slack event listeners on Bolt AsyncApp."""

    @app.event("message")
    async def handle_message(event: dict[str, Any], client: Any = None) -> None:
        """Handle incoming Slack message events (created, updated, deleted)."""
        subtype = event.get("subtype")

        # We only process standard messages, edits, and deletes.
        if subtype not in (None, "bot_message", "message_changed", "message_deleted"):
            logger.debug("slack.message.ignored_subtype", subtype=subtype)
            return

        team_id = (
            event.get("team")
            or event.get("team_id")
            or event.get("user_team")
            or event.get("message", {}).get("team")
            or event.get("message", {}).get("team_id")
            or event.get("previous_message", {}).get("team")
            or event.get("previous_message", {}).get("team_id")
        )
        if not team_id:
            logger.warning("slack.message.no_team", event=event)
            return

        # Lookup tenant_id and bot_token from team_id
        async with app_bypass_rls_context(db_engine) as conn:
            stmt = select(
                SlackInstallation.tenant_id,
                SlackInstallation.bot_token,
            ).where(SlackInstallation.team_id == team_id)
            res = await conn.execute(stmt)
            row = res.fetchone()
            if not row:
                logger.warning("slack.message.no_installation", team_id=team_id)
                return
            tenant_id: UUID = row[0]
            bot_token = decrypt_secret(row[1])

        if not await scope.allows(
            db_engine, tenant_id, connection_key("slack", team_id), event.get("channel")
        ):
            return

        if client is None:
            client = AsyncWebClient(token=bot_token, base_url=get_settings().slack_api_url)

        # Extract timestamp and resolve event action/metadata
        if subtype == "message_changed":
            msg_data = event.get("message", {})
            original_ts = msg_data.get("ts")
            edited_ts = (
                msg_data.get("edited", {}).get("ts") or event.get("event_ts") or event.get("ts")
            )
            if not isinstance(original_ts, str) or not isinstance(edited_ts, str):
                logger.warning("slack.message.changed_no_ts", event=event)
                return
            try:
                happened_at = datetime.fromtimestamp(float(edited_ts), tz=UTC)
            except (ValueError, TypeError):
                happened_at = datetime.now(UTC)
            event_action = EventAction.UPDATED

            # Content
            text = msg_data.get("text") or ""
            if is_generic_or_empty(text):
                blocks = msg_data.get("blocks")
                if blocks:
                    extracted = extract_text_from_blocks(blocks)
                    if extracted:
                        text = extracted
            content_raw = text
            content_clean, entities = normalize_slack_text(content_raw)

            # Author
            user_id = msg_data.get("user") or event.get("user") or "unknown"
            bot_id = msg_data.get("bot_id") or event.get("bot_id")

        elif subtype == "message_deleted":
            original_ts = event.get("deleted_ts")
            deleted_at_ts = event.get("event_ts") or event.get("ts")
            if not isinstance(original_ts, str) or not isinstance(deleted_at_ts, str):
                logger.warning("slack.message.deleted_no_ts", event=event)
                return
            try:
                happened_at = datetime.fromtimestamp(float(deleted_at_ts), tz=UTC)
            except (ValueError, TypeError):
                happened_at = datetime.now(UTC)
            event_action = EventAction.DELETED
            content_raw = ""
            content_clean = ""
            entities = []

            # Author
            user_id = (
                event.get("user") or event.get("previous_message", {}).get("user") or "unknown"
            )
            bot_id = event.get("bot_id") or event.get("previous_message", {}).get("bot_id")

        else:  # None or bot_message (new message)
            original_ts = event.get("ts")
            if not isinstance(original_ts, str):
                logger.warning("slack.message.no_ts", event=event)
                return
            try:
                happened_at = datetime.fromtimestamp(float(original_ts), tz=UTC)
            except (ValueError, TypeError):
                happened_at = datetime.now(UTC)
            event_action = EventAction.CREATED

            # Content
            text = event.get("text") or ""
            if is_generic_or_empty(text):
                blocks = event.get("blocks")
                if blocks:
                    extracted = extract_text_from_blocks(blocks)
                    if extracted:
                        text = extracted
            content_raw = text
            content_clean, entities = normalize_slack_text(content_raw)

            # Author
            user_id = event.get("user") or "unknown"
            bot_id = event.get("bot_id")

        thread_ts = event.get("thread_ts") or event.get("message", {}).get("thread_ts")

        internal_id, email, display_name = None, None, None
        if user_id and user_id != "unknown":
            from checkcord.adapters.chat.core.identity import resolve_author_identity

            internal_id, email, display_name = await resolve_author_identity(
                db_engine=db_engine,
                tenant_id=tenant_id,
                platform="slack",
                native_id=user_id,
            )

        # Download text attachments
        if event_action != EventAction.DELETED:
            files = None
            if subtype == "message_changed":
                files = event.get("message", {}).get("files") or event.get("files")
            else:
                files = event.get("files")

            if files:
                whitelist = {
                    ".txt",
                    ".log",
                    ".py",
                    ".js",
                    ".ts",
                    ".go",
                    ".rs",
                    ".c",
                    ".cpp",
                    ".h",
                    ".sh",
                    ".yaml",
                    ".yml",
                    ".json",
                    ".ini",
                    ".conf",
                    ".sql",
                    ".md",
                }
                for file_obj in files:
                    mimetype = file_obj.get("mimetype") or ""
                    filename = file_obj.get("name") or "untitled"

                    is_text = False
                    if mimetype.startswith("text/") or mimetype == "application/json":
                        is_text = True
                    else:
                        ext = os.path.splitext(filename.lower())[1]
                        if ext in whitelist:
                            is_text = True

                    if is_text:
                        size = file_obj.get("size") or 0
                        if size <= 5 * 1024 * 1024:
                            url = file_obj.get("url_private")
                            if url:
                                try:
                                    async with aiohttp.ClientSession() as session:
                                        headers = {"Authorization": f"Bearer {bot_token}"}
                                        async with session.get(url, headers=headers) as response:
                                            if response.status == 200:
                                                text_content = await response.text()
                                                content_raw += (
                                                    f"\n\n=== Attachment: {filename} ===\n"
                                                    f"{text_content}"
                                                )
                                            else:
                                                logger.error(
                                                    "slack.attachment.download_failed",
                                                    filename=filename,
                                                    status=response.status,
                                                    url=url,
                                                )
                                                content_raw += (
                                                    f"\n\n[Warning: Failed to download "
                                                    f"attachment {filename}]"
                                                )
                                except Exception as e:
                                    logger.error(
                                        "slack.attachment.download_error",
                                        filename=filename,
                                        error=str(e),
                                        url=url,
                                    )
                                    content_raw += (
                                        f"\n\n[Warning: Failed to download attachment {filename}]"
                                    )
                        else:
                            logger.info(
                                "slack.attachment.too_large",
                                filename=filename,
                                size=size,
                            )

                # Recompute content_clean and entities after adding attachment content
                content_clean, entities = normalize_slack_text(content_raw)

        channel_id = event.get("channel") or "unknown"
        access_control = await resolve_slack_audience(
            client=client,
            redis=bus.redis,
            db_engine=db_engine,
            tenant_id=tenant_id,
            channel_id=channel_id,
            team_id=team_id,
        )
        channel_name = await resolve_channel_name(db_engine, tenant_id, "slack", channel_id)

        raw_payload = RawChatPayload(
            tenant_id=tenant_id,
            platform=SourcePlatform.SLACK,
            channel_id=channel_id,
            channel_name=channel_name,
            author_id=user_id,
            author_email=email,
            author_name=display_name,
            message_id=original_ts,
            thread_ts=thread_ts,
            happened_at=happened_at,
            content_raw=content_raw,
            content_clean=content_clean,
            raw_event=event,
            access_control=access_control,
            connection=connection_key("slack", team_id),
        )

        # Check bot noise filtering before publishing. Skip filtering for deleted messages.
        if event_action != EventAction.DELETED:
            username = (
                event.get("message", {}).get("username")
                if subtype == "message_changed"
                else event.get("username")
            )
            if should_filter_message(
                platform=SourcePlatform.SLACK,
                bot_id=bot_id,
                username=username,
                text=content_raw,
            ):
                logger.info(
                    "slack.message.bot_filtered",
                    message_id=original_ts,
                    bot_id=bot_id,
                    username=username,
                )
                return

        # Publish to raw stream
        try:
            msg_id = await bus.publish(topic="events.raw.chat", event=raw_payload)
            logger.info(
                "slack.message.published",
                payload_id=str(raw_payload.payload_id),
                msg_id=msg_id,
                action=event_action.value,
            )
        except Exception as e:
            logger.error(
                "slack.message.publish_failed",
                error=str(e),
                payload_id=str(raw_payload.payload_id),
            )

    async def refresh_channel(body: dict[str, Any], event: dict[str, Any], kind: str) -> None:
        team_id = body.get("team_id") or event.get("team")
        channel = event.get("channel")
        channel_id = channel.get("id") if isinstance(channel, dict) else channel
        if not team_id or not channel_id:
            return
        async with app_bypass_rls_context(db_engine) as conn:
            row = (
                await conn.execute(
                    select(SlackInstallation.tenant_id, SlackInstallation.bot_token).where(
                        SlackInstallation.team_id == team_id
                    )
                )
            ).first()
        if row is None:
            return
        client = AsyncWebClient(token=decrypt_secret(row[1]), base_url=get_settings().slack_api_url)
        await audiences.refresh_stored(
            db_engine,
            row[0],
            audiences.SLACK,
            str(channel_id),
            trigger=f"slack:{kind}",
            reader=lambda: read_slack_channel(client, str(channel_id)),
            details={"team_id": team_id},
            redis=bus.redis,
        )

    @app.event("member_joined_channel")
    async def handle_member_joined(body: dict[str, Any], event: dict[str, Any]) -> None:
        await refresh_channel(body, event, "member_joined_channel")

    @app.event("member_left_channel")
    async def handle_member_left(body: dict[str, Any], event: dict[str, Any]) -> None:
        await refresh_channel(body, event, "member_left_channel")

    @app.event("channel_converted_to_private")
    async def handle_converted_to_private(body: dict[str, Any], event: dict[str, Any]) -> None:
        await refresh_channel(body, event, "channel_converted_to_private")

    @app.event("app_uninstalled")
    async def handle_app_uninstalled(body: dict[str, Any]) -> None:
        """Handle when the Slack workspace uninstalls the app."""
        team_id = body.get("team_id")
        logger.info("slack.event.app_uninstalled", team_id=team_id)
        if team_id:
            store = PostgresInstallationStore(db_engine)
            await store.async_delete_all(enterprise_id=None, team_id=team_id)

    @app.event("tokens_revoked")
    async def handle_tokens_revoked(body: dict[str, Any], event: dict[str, Any]) -> None:
        """Handle when the user/bot tokens are revoked for the app."""
        team_id = body.get("team_id")
        logger.info("slack.event.tokens_revoked", team_id=team_id, slack_event=event)
        if team_id:
            store = PostgresInstallationStore(db_engine)
            await store.async_delete_all(enterprise_id=None, team_id=team_id)

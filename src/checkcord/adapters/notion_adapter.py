"""Notion page blocks as documents."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import structlog

from checkcord.core import http_retry
from checkcord.core.config import get_settings
from checkcord.schemas.document import Document, DocumentBlock

logger = structlog.get_logger(__name__)


class NotionAdapter:
    """Reads Notion page blocks and converts them to documents."""

    def __init__(self, access_token: str, client: httpx.AsyncClient | None = None) -> None:
        self.access_token = access_token
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client and not self._client.is_closed:
            return self._client
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }
        return httpx.AsyncClient(headers=headers, timeout=30.0)

    async def fetch_page_blocks(self, block_id: str) -> list[dict[str, Any]]:
        """Fetch every child block of a block or page from the Notion API."""
        url = f"{get_settings().notion_api_url}/v1/blocks/{block_id}/children"
        client = await self._get_client()
        blocks: list[dict[str, Any]] = []
        params: dict[str, Any] = {"page_size": 100}
        while True:
            response = await http_retry.send("notion", client, "GET", url, params=params)
            response.raise_for_status()
            data = response.json()
            blocks.extend(cast(list[dict[str, Any]], data.get("results", [])))
            if not data.get("has_more") or not data.get("next_cursor"):
                return blocks
            params = {"page_size": 100, "start_cursor": data["next_cursor"]}

    def parse_rich_text(self, rich_text_array: list[dict[str, Any]]) -> str:
        """Extract plain text from Notion rich text array."""
        return "".join(
            item.get("plain_text", "") for item in rich_text_array if isinstance(item, dict)
        )

    def parse_notion_block(self, block: dict[str, Any]) -> DocumentBlock:
        """Recursively parse a single Notion block into a DocumentBlock with graceful fallback."""
        block_type = block.get("type", "unknown")
        doc_block = DocumentBlock(type=block_type, raw_json=block)

        try:
            type_data = block.get(block_type, {})
            if isinstance(type_data, dict):
                rich_text = type_data.get("rich_text", [])
                doc_block.text = self.parse_rich_text(rich_text)

                if block_type in ("heading_1", "heading_2", "heading_3"):
                    level = int(block_type.split("_")[1])
                    doc_block.metadata["level"] = level
                elif block_type == "code":
                    lang = type_data.get("language", "plain text")
                    doc_block.metadata["language"] = lang
                    doc_block.text = f"```{lang}\n{doc_block.text}\n```"

            if block.get("has_children", False):
                # Placeholder for nested children if pre-fetched
                children = block.get("children_prefetched", [])
                doc_block.children = [self.parse_notion_block(child) for child in children]
        except Exception as err:
            logger.warning(
                "notion_parser_unhandled_block_fallback", block_type=block_type, error=str(err)
            )
            doc_block.type = "unhandled_node"
            fallback_text = json.dumps(block, indent=2)
            doc_block.text = f"```unhandled-node-type: {block_type}\n{fallback_text}\n```"

        return doc_block

    def parse_page_to_document(
        self, page_id: str, title: str, raw_blocks: list[dict[str, Any]]
    ) -> Document:
        """Convert pre-fetched Notion blocks into a unified Document AST."""
        blocks = [self.parse_notion_block(b) for b in raw_blocks]
        full_markdown = f"# {title}\n\n" + "\n\n".join(b.text for b in blocks if b.text)

        return Document(
            title=title,
            source_platform="notion",
            source_id=page_id,
            blocks=blocks,
            raw_markdown_fallback=full_markdown,
        )

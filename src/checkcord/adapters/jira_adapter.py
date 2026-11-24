"""Jira issue reads and ADF to document conversion."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import structlog

from checkcord.core import http_retry
from checkcord.schemas.document import Document, DocumentBlock

logger = structlog.get_logger(__name__)


def jira_base_url(jira_host: str) -> str:
    return jira_host if "://" in jira_host else f"https://{jira_host}"


class JiraAdapter:
    """Reads Jira issues and converts their ADF to documents."""

    def __init__(
        self,
        jira_host: str,
        personal_access_token: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.jira_host = jira_host.rstrip("/")
        self.personal_access_token = personal_access_token
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client and not self._client.is_closed:
            return self._client
        headers = {
            "Authorization": f"Bearer {self.personal_access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        return httpx.AsyncClient(headers=headers, timeout=30.0)

    async def fetch_issue(self, issue_key: str) -> dict[str, Any]:
        """Fetch raw issue payload from Jira REST API v3 asynchronously."""
        url = f"{jira_base_url(self.jira_host)}/rest/api/3/issue/{issue_key}"
        client = await self._get_client()
        response = await http_retry.send("jira", client, "GET", url)
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    def parse_adf_node(self, node: dict[str, Any]) -> DocumentBlock:
        """Recursively parse an ADF node into a DocumentBlock.

        Falls back gracefully on unhandled node types.
        """
        node_type = node.get("type", "unknown")
        block = DocumentBlock(type=node_type, raw_json=node)

        try:
            if node_type == "text":
                block.text = node.get("text", "")
            elif node_type in ("paragraph", "heading"):
                level = node.get("attrs", {}).get("level", 1)
                block.metadata["level"] = level
                child_blocks = [self.parse_adf_node(child) for child in node.get("content", [])]
                block.children = child_blocks
                block.text = "".join(child.text for child in child_blocks)
            elif node_type in ("bulletList", "orderedList"):
                child_blocks = [self.parse_adf_node(child) for child in node.get("content", [])]
                block.children = child_blocks
                block.text = "\n".join(child.text for child in child_blocks)
            elif node_type == "listItem":
                child_blocks = [self.parse_adf_node(child) for child in node.get("content", [])]
                block.children = child_blocks
                block.text = " ".join(child.text for child in child_blocks)
            elif node_type == "codeBlock":
                lang = node.get("attrs", {}).get("language", "")
                block.metadata["language"] = lang
                child_blocks = [self.parse_adf_node(child) for child in node.get("content", [])]
                code_text = "".join(child.text for child in child_blocks)
                block.text = f"```{lang}\n{code_text}\n```"
            elif node_type in ("blockquote", "panel"):
                panel_type = node.get("attrs", {}).get("panelType", "info")
                block.metadata["panel_type"] = panel_type
                child_blocks = [self.parse_adf_node(child) for child in node.get("content", [])]
                block.children = child_blocks
                block.text = "\n".join(child.text for child in child_blocks)
            elif node_type in ("table", "tableRow", "tableHeader", "tableCell"):
                child_blocks = [self.parse_adf_node(child) for child in node.get("content", [])]
                block.children = child_blocks
                block.text = " | ".join(child.text for child in child_blocks if child.text)
            elif "content" in node and isinstance(node["content"], list):
                child_blocks = [self.parse_adf_node(child) for child in node["content"]]
                block.children = child_blocks
                block.text = " ".join(child.text for child in child_blocks if child.text)
            else:
                # Leaf node without explicit handling
                block.text = node.get("text", "")
        except Exception as err:
            logger.warning(
                "adf_parser_unhandled_node_fallback", node_type=node_type, error=str(err)
            )
            block.type = "unhandled_node"
            fallback_text = json.dumps(node, indent=2)
            block.text = f"```unhandled-node-type: {node_type}\n{fallback_text}\n```"

        return block

    def parse_issue_to_document(self, issue_payload: dict[str, Any]) -> Document:
        """Convert a complete Jira issue JSON payload into a unified Document AST."""
        fields = issue_payload.get("fields", {})
        summary = fields.get("summary", "Untitled Jira Ticket")
        issue_key = issue_payload.get("key", "UNKNOWN-KEY")

        description_adf = fields.get("description") or {}
        blocks: list[DocumentBlock] = []

        if isinstance(description_adf, dict) and description_adf.get("type") == "doc":
            for content_node in description_adf.get("content", []):
                blocks.append(self.parse_adf_node(content_node))
        elif isinstance(description_adf, str):
            blocks.append(DocumentBlock(type="paragraph", text=description_adf))

        # Include comment blocks
        comments_data = fields.get("comment", {}).get("comments", [])
        for comment in comments_data:
            author_name = comment.get("author", {}).get("displayName", "Unknown Author")
            body_adf = comment.get("body")
            if isinstance(body_adf, dict) and body_adf.get("type") == "doc":
                comment_blocks = [self.parse_adf_node(c) for c in body_adf.get("content", [])]
                comment_text = "\n".join(cb.text for cb in comment_blocks)
            else:
                comment_text = str(body_adf or "")

            blocks.append(
                DocumentBlock(
                    type="comment",
                    text=f"Comment by {author_name}:\n{comment_text}",
                    metadata={"author": author_name, "id": comment.get("id")},
                )
            )

        full_markdown = f"# {issue_key}: {summary}\n\n" + "\n\n".join(
            b.text for b in blocks if b.text
        )
        return Document(
            title=f"[{issue_key}] {summary}",
            source_platform="jira",
            source_id=issue_key,
            blocks=blocks,
            metadata={
                "issue_type": fields.get("issuetype", {}).get("name"),
                "status": fields.get("status", {}).get("name"),
                "project": fields.get("project", {}).get("key"),
            },
            raw_markdown_fallback=full_markdown,
        )

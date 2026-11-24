"""Linear issues as documents."""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from checkcord.schemas.document import Document, DocumentBlock

logger = structlog.get_logger(__name__)

try:
    import mistune

    HAS_MISTUNE = True
except ImportError:
    HAS_MISTUNE = False


class LinearAdapter:
    """Converts Linear issues and their Markdown to documents."""

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self.api_key = api_key
        self._client = client
        self._markdown_parser = mistune.create_markdown(renderer="ast") if HAS_MISTUNE else None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client and not self._client.is_closed:
            return self._client
        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }
        return httpx.AsyncClient(headers=headers, timeout=30.0)

    def parse_markdown_to_blocks(self, markdown_text: str) -> list[DocumentBlock]:
        """Parse raw markdown into DocumentBlocks using mistune AST renderer or line fallback."""
        if not markdown_text:
            return []

        if not HAS_MISTUNE or self._markdown_parser is None:
            # Fallback line parser when mistune is not available
            blocks: list[DocumentBlock] = []
            paragraphs = markdown_text.split("\n\n")
            for p in paragraphs:
                p_clean = p.strip()
                if not p_clean:
                    continue
                block_type = "heading" if p_clean.startswith("#") else "paragraph"
                blocks.append(DocumentBlock(type=block_type, text=p_clean))
            return blocks

        try:
            tokens = self._markdown_parser(markdown_text)
            blocks = []
            for token in tokens:
                token_type = token.get("type", "paragraph")
                text_content = ""

                if "children" in token and isinstance(token["children"], list):
                    text_content = "".join(
                        c.get("text", "") if isinstance(c, dict) else str(c)
                        for c in token["children"]
                    )
                elif "text" in token:
                    text_content = str(token["text"])

                blocks.append(
                    DocumentBlock(
                        type=token_type,
                        text=text_content,
                        raw_json=token if isinstance(token, dict) else {"raw": str(token)},
                    )
                )
            return blocks
        except Exception as err:
            logger.warning("linear_markdown_parser_fallback", error=str(err))
            return [
                DocumentBlock(
                    type="unhandled_node",
                    text=f"```unhandled-markdown\n{markdown_text}\n```",
                    raw_json={"raw": markdown_text},
                )
            ]

    def parse_issue_to_document(self, issue_data: dict[str, Any]) -> Document:
        """Convert Linear issue data dictionary into a unified Document AST."""
        identifier = issue_data.get("identifier", "LIN-0")
        title = issue_data.get("title", "Untitled Linear Issue")
        description = issue_data.get("description", "")

        blocks = self.parse_markdown_to_blocks(description)

        # Parse comments
        comments = issue_data.get("comments", {}).get("nodes", [])
        for comment in comments:
            author = comment.get("user", {}).get("name", "Unknown User")
            body = comment.get("body", "")
            comment_blocks = self.parse_markdown_to_blocks(body)
            comment_text = "\n".join(b.text for b in comment_blocks)
            blocks.append(
                DocumentBlock(
                    type="comment",
                    text=f"Comment by {author}:\n{comment_text}",
                    metadata={"author": author, "id": comment.get("id")},
                )
            )

        full_markdown = f"# [{identifier}] {title}\n\n{description}"
        return Document(
            title=f"[{identifier}] {title}",
            source_platform="linear",
            source_id=identifier,
            blocks=blocks,
            metadata={
                "state": issue_data.get("state", {}).get("name"),
                "priority": issue_data.get("priority"),
            },
            raw_markdown_fallback=full_markdown,
        )

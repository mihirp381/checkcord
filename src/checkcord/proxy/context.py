import os

import structlog

from checkcord.core.redaction import redact_regex

logger = structlog.get_logger(__name__)

# Hard cutoff at 80,000 characters (~20,000 tokens) to prevent context window overflow
MAX_CONTEXT_CHARS = 80000


class DynamicContextCache:
    """Caches the contents of .context/ARCHITECTURE.md in memory with hot reloading.

    The file is written from stored redacted text; the regex pass runs again
    once per reload.
    """

    def __init__(self, relative_path: str = os.path.join(".context", "ARCHITECTURE.md")) -> None:
        self.relative_path = relative_path
        self._cached_content: str | None = None
        self._last_mtime: float = -1.0
        self._file_existed: bool = False

    async def get_context_string(self) -> str | None:
        """Resolves the context file from current working directory, checks mtime,
        and returns formatted, PII-redacted content."""
        target_path = os.path.abspath(os.path.join(os.getcwd(), self.relative_path))  # noqa: ASYNC240

        try:
            if not os.path.exists(target_path):  # noqa: ASYNC240
                if self._file_existed:
                    # File was deleted; invalidate cache
                    logger.info("context_file_deleted_invalidating_cache", path=target_path)
                    self._cached_content = None
                    self._last_mtime = -1.0
                    self._file_existed = False
                return None

            self._file_existed = True
            stat = os.stat(target_path)
            current_mtime = stat.st_mtime

            # If file has changed, reload it
            if current_mtime != self._last_mtime:
                logger.info(
                    "context_file_changed_reloading",
                    path=target_path,
                    old_mtime=self._last_mtime,
                    new_mtime=current_mtime,
                )

                with open(target_path, encoding="utf-8") as f:  # noqa: ASYNC101, ASYNC230
                    content = f.read()

                # Handle potential truncation if file is too large
                if len(content) > MAX_CONTEXT_CHARS:
                    logger.warning(
                        "context_file_too_large_truncating",
                        path=target_path,
                        original_len=len(content),
                        limit=MAX_CONTEXT_CHARS,
                    )
                    content = content[:MAX_CONTEXT_CHARS] + "\n...[TRUNCATED BY PROXY]"

                content = redact_regex(content)

                # Format within the XML tags
                self._cached_content = f"<checkcord_context>\n{content}\n</checkcord_context>"
                self._last_mtime = current_mtime

            return self._cached_content

        except Exception as e:
            # Concurrency/Race conditions or permission errors: fail-safe by returning
            # the previously cached content if available
            logger.error("context_read_failed_falling_back", path=target_path, error=str(e))
            return self._cached_content

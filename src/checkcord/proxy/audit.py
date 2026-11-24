import difflib
import json
import logging
import os
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def generate_json_diff(original: dict[str, Any], mutated: dict[str, Any]) -> str:
    """Computes a unified line-by-line diff of two sorted JSON representations."""
    orig_str = json.dumps(original, indent=2, sort_keys=True)
    mutated_str = json.dumps(mutated, indent=2, sort_keys=True)

    orig_lines = orig_str.splitlines()
    mutated_lines = mutated_str.splitlines()

    diff = difflib.unified_diff(
        orig_lines,
        mutated_lines,
        fromfile="Original Payload",
        tofile="Mutated Payload",
        lineterm="",
    )
    return "\n".join(diff)


class AuditLogger:
    """Logs unified JSON payload diffs to a rotating local file."""

    def __init__(self, relative_log_dir: str = os.path.join(".context", "logs")) -> None:
        self.relative_log_dir = relative_log_dir
        self._logger: logging.Logger | None = None
        self._enabled = True

    def _setup_logger(self) -> bool:
        """Initializes the rotating file logger. Returns True if successful, False otherwise."""
        if self._logger is not None:
            return self._enabled

        log_dir = os.path.abspath(os.path.join(os.getcwd(), self.relative_log_dir))
        log_file = os.path.join(log_dir, "prompt_diffs.log")

        try:
            os.makedirs(log_dir, exist_ok=True)

            # Setup standard logger to control RotatingFileHandler
            audit_logger = logging.getLogger("checkcord.proxy.audit")
            audit_logger.setLevel(logging.INFO)
            audit_logger.propagate = False  # Avoid duplicating messages in main app logs

            # Clear any existing handlers to avoid logging to outdated directories
            for handler in list(audit_logger.handlers):
                handler.close()
                audit_logger.removeHandler(handler)

            # 10MB limit per file, 3 backups (total ~40MB max)
            handler = RotatingFileHandler(
                log_file, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            # Keep logs clean; no prefix since we format headers ourselves
            formatter = logging.Formatter("%(message)s")
            handler.setFormatter(formatter)
            audit_logger.addHandler(handler)

            self._logger = audit_logger
            self._enabled = True
            logger.info("audit_logger_initialized", path=log_file)
            return True
        except Exception as e:
            logger.error("audit_logger_setup_failed_disabling", error=str(e))
            self._enabled = False
            return False

    def log_diff(self, path: str, original: dict[str, Any], mutated: dict[str, Any]) -> None:
        """Generates and writes a unified diff of the original vs mutated payload
        to the audit log."""
        if not self._setup_logger():
            return

        try:
            diff = generate_json_diff(original, mutated)
            if not diff:
                # No change, skip logging
                return

            timestamp = datetime.now(UTC).isoformat()
            model = original.get("model") or mutated.get("model") or "unknown-model"

            log_entry = (
                f"======================================================================\n"
                f"TIMESTAMP: {timestamp}Z\n"
                f"PATH:      {path}\n"
                f"MODEL:     {model}\n"
                f"----------------------------------------------------------------------\n"
                f"{diff}\n"
                f"======================================================================\n\n"
            )

            if self._logger:
                self._logger.info(log_entry.strip("\n"))
        except Exception as e:
            logger.error("audit_log_failed", error=str(e))

    def close(self) -> None:
        """Closes and releases handlers for the audit logger."""
        if self._logger:
            for handler in list(self._logger.handlers):
                handler.close()
                self._logger.removeHandler(handler)
            self._logger = None

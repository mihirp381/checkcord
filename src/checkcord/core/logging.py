import logging
import sys
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, merge_contextvars


def setup_logging(log_level: str = "INFO", json_format: bool = True) -> None:
    """Configure structlog and standard logging."""

    level = getattr(logging, log_level.upper(), logging.INFO)
    root = logging.getLogger()
    # FastMCP adds a RichHandler at import, which wraps each JSON line over several lines.
    for handler in [h for h in root.handlers if type(h).__name__ == "RichHandler"]:
        root.removeHandler(handler)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=level,
    )
    root.setLevel(level)

    shared_processors: list[Any] = [
        merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.CallsiteParameterAdder(
            {
                structlog.processors.CallsiteParameter.FILENAME,
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.LINENO,
            }
        ),
    ]

    if json_format:
        processors = shared_processors + [
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a bound logger instance."""
    from typing import cast

    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))


# Export contextvar helpers
__all__ = ["setup_logging", "get_logger", "bind_contextvars", "clear_contextvars"]

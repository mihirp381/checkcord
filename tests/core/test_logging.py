import logging

from checkcord.core.logging import setup_logging


class RichHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        pass


def test_setup_logging_drops_rich_handler() -> None:
    root = logging.getLogger()
    rich = RichHandler()
    root.addHandler(rich)
    try:
        setup_logging()
        assert rich not in root.handlers
    finally:
        if rich in root.handlers:
            root.removeHandler(rich)

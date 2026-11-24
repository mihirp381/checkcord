import pytest

from checkcord.adapters.meetings.providers.zoom.webhook import _timestamp_is_fresh


@pytest.mark.parametrize(
    ("timestamp", "fresh"),
    [("1000", True), ("1300", True), ("700", True), ("1301", False), ("699", False)],
)
def test_freshness_window(timestamp: str, fresh: bool) -> None:
    assert _timestamp_is_fresh(timestamp, now=1000.0) is fresh


@pytest.mark.parametrize("timestamp", ["", "abc", "1.5", "1e9"])
def test_non_integer_timestamp_rejected(timestamp: str) -> None:
    assert _timestamp_is_fresh(timestamp, now=1000.0) is False

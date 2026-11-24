import json
import logging
from typing import Any

import pytest

from checkcord.core.telemetry import increment_counter


def test_increment_counter(caplog: pytest.LogCaptureFixture) -> None:
    """Test that increment_counter outputs the expected JSON log format with tags."""
    caplog.set_level(logging.INFO)
    increment_counter("heuristic_true_positive", {"platform": "slack", "user_id": "U123"})

    assert len(caplog.records) >= 1
    log_entry: Any = None
    for record in caplog.records:
        msg = record.getMessage()
        try:
            data = json.loads(msg)
            if data.get("event") == "metric_event":
                log_entry = data
                break
        except (json.JSONDecodeError, AttributeError):
            pass

    assert log_entry is not None, f"Could not find metric_event in caplog: {caplog.records}"
    assert log_entry["event"] == "metric_event"
    assert log_entry["metric_name"] == "heuristic_true_positive"
    assert log_entry["metric_type"] == "counter"
    assert log_entry["tags"] == {"platform": "slack", "user_id": "U123"}


def test_increment_counter_no_tags(caplog: pytest.LogCaptureFixture) -> None:
    """Test that increment_counter outputs the expected JSON log format when tags is None."""
    caplog.set_level(logging.INFO)
    increment_counter("heuristic_false_positive")

    assert len(caplog.records) >= 1
    log_entry: Any = None
    for record in caplog.records:
        msg = record.getMessage()
        try:
            data = json.loads(msg)
            if data.get("event") == "metric_event":
                log_entry = data
                break
        except (json.JSONDecodeError, AttributeError):
            pass

    assert log_entry is not None, f"Could not find metric_event in caplog: {caplog.records}"
    assert log_entry["event"] == "metric_event"
    assert log_entry["metric_name"] == "heuristic_false_positive"
    assert log_entry["metric_type"] == "counter"
    assert log_entry["tags"] == {}

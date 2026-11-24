from datetime import timedelta

from checkcord.storage import event_pipeline


def test_retry_delay_doubles_and_caps() -> None:
    delays = [event_pipeline.retry_delay(n) for n in (1, 2, 3, 4)]
    assert delays == [timedelta(seconds=s) for s in (30, 60, 120, 240)]
    assert event_pipeline.retry_delay(50) == event_pipeline.BACKOFF_MAX
    assert event_pipeline.retry_delay(10_000) == event_pipeline.BACKOFF_MAX


def test_retry_schedule_covers_the_window() -> None:
    total, attempts = timedelta(0), 0
    while total < event_pipeline.RETRY_WINDOW:
        attempts += 1
        total += event_pipeline.retry_delay(attempts)
    assert 25 < attempts < 40


def test_describe_error_redacts_and_truncates() -> None:
    fake_key = "sk-" + "Qq7Ww6Ee" * 4
    text = event_pipeline.describe_error(RuntimeError(f"401 for key {fake_key} " + "x" * 900))
    assert text.startswith("RuntimeError: 401 for key [REDACTED:TOKEN]")
    assert fake_key not in text
    assert len(text) == event_pipeline.ERROR_MAX_CHARS

import time
from pathlib import Path
from uuid import uuid4

import pytest

from checkcord.connectors.signed_webhook.routes import signature
from sim import harness


def test_secrets_are_masked_in_nested_output() -> None:
    s = harness.Secrets()
    s.add("short", "xoxb-0123456789abcdef", None)
    masked = s.mask({"a": ["token xoxb-0123456789abcdef here"], "xoxb-0123456789abcdef": 1})
    assert masked == {"a": ["token <secret:21> here"], "<secret:21>": 1}
    assert s.mask("short") == "short"


def test_longer_secret_masked_before_its_prefix() -> None:
    s = harness.Secrets()
    s.add("abcdefgh", "abcdefghijkl")
    assert s.mask("abcdefghijkl") == "<secret:12>"


def test_test_strings_are_built_at_run_time() -> None:
    key = harness.aws_style_key()
    assert key.startswith("AKIA") and len(key) == 20 and key.isupper()
    assert harness.webhook_test_secret().startswith("whsec_")
    assert harness.aws_style_key() != key


def test_metrics_parsing_and_open_items() -> None:
    text = "\n".join(
        [
            "# HELP checkcord_chat_open open chat buffers",
            'checkcord_chat_open{kind="buffer"} 2.0',
            'checkcord_chat_open{kind="tree"} 0.0',
            'checkcord_stream_lag{stream="events"} 0',
            "checkcord_events_unfinished 1",
            'checkcord_http_requests_total{path="/x"} 9',
        ]
    )
    rows = harness.parse_metrics(text)
    assert ("checkcord_chat_open", {"kind": "buffer"}, 2.0) in rows
    assert harness.open_items(text) == {
        "checkcord_chat_open{kind=buffer}": 2.0,
        "checkcord_events_unfinished{}": 1.0,
    }


def test_hit_by_id_or_text() -> None:
    body = {
        "results": [
            {"id": "r-1", "summary": "Every POST endpoint takes an Idempotency-Key header."}
        ]
    }
    assert harness.hit(body, "r-1")
    assert harness.hit(body, None, "every post endpoint takes an idempotency key header")
    assert not harness.hit(body, "r-2", "quote_total returns integer cents")
    assert not harness.hit(body, None, None)
    a, b = str(uuid4()), str(uuid4())
    prefix = "Business requirement from Slack channel C0ENG00001: "
    other = {"results": [{"id": a, "summary": prefix + "refunds need a second approver."}]}
    assert not harness.hit(other, b, prefix + "money is stored as integer cents.")
    assert harness.hit(other, a, "anything")
    text = prefix + "refunds over 500 EUR need a second approver in the back office."
    assert harness.hit(f"Found: {text}", b, text)
    assert not harness.hit(f"Found: {prefix} the p99 latency SLO is 300 ms.", b, text)
    assert harness.norm("A-b  C!") == "a b c"


def test_cli_counts_and_doctor_lines() -> None:
    out = "Would purge:\n  events: 4\n  records deleted: 1\n  code symbols: 0\n"
    assert harness.cli_counts(out) == {"events": 4, "records_deleted": 1, "code_symbols": 0}
    lines = harness.doctor_lines("OK   database\nWARN model: down\n  detail\nFAIL redis")
    assert lines == {"OK": ["OK   database"], "WARN": ["WARN model: down"], "FAIL": ["FAIL redis"]}


def test_signed_headers_match_the_product() -> None:
    at = time.time()
    body = b'{"id": "1"}'
    headers = harness.signed_headers("s3cret-value", body, at)
    assert headers["X-Checkcord-Signature"] == signature(
        "s3cret-value", headers["X-Checkcord-Timestamp"], body
    )


def test_cli_output_parsers() -> None:
    out = (
        "Webhook secret (shown once, not retrievable later):\n\n    abc123\n\n"
        "  Payload URL:  http://localhost:8410/api/v1/webhooks/github\n"
    )
    assert harness.secret_after("Webhook secret", out) == "abc123"
    assert harness.url_after("Payload URL:", out) == "http://localhost:8410/api/v1/webhooks/github"
    assert harness.secret_after("Missing", out) is None


def test_env_file_is_marked_and_overrides_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env.local-models.example").write_text(
        "# comment\nLLM_BASE_URL=http://old\nKEEP=1\n"
    )
    monkeypatch.setattr(harness, "ROOT", tmp_path)
    monkeypatch.setattr(harness, "ENV", tmp_path / ".env")
    secrets = {
        k: "x" * 12
        for k in (
            "slack_client_id",
            "slack_client_secret",
            "slack_signing_secret",
            "slack_app_token",
            "oidc_client_id",
            "oidc_client_secret",
        )
    }
    overrides = harness.env_overrides(secrets)
    harness.write_env(overrides)
    text = (tmp_path / ".env").read_text()
    lines = text.splitlines()
    assert lines[0] == harness.ENV_MARK
    assert "KEEP=1" in lines and "LLM_BASE_URL=http://old" not in lines
    assert overrides["LLM_BASE_URL"].endswith("/model/v1")
    assert overrides["CHECKCORD_API_PORT"] == "8410"
    assert "8000" not in overrides["PUBLIC_BASE_URL"]
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


def test_scenarios_register_in_order() -> None:
    chosen = harness.select("easy", None)
    ids = [s.id for s in chosen]
    assert ids[0] == "E01" and ids[-1] == "E18"
    assert sorted(ids) == [f"E{n:02d}" for n in range(1, 21)]
    hard = [s.id for s in harness.select("hard", None)]
    assert hard[-2:] == ["H13", "H22"]
    assert sorted(hard) == sorted(f"H{n:02d}" for n in range(1, 28))
    every = [s.id for s in harness.select("all", None)]
    assert every[: len(ids) - 1] == ids[:-1] and every[-3:] == ["E18", "H13", "H22"]
    with pytest.raises(SystemExit):
        harness.select("easy", ["E99"])

import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from checkcord.connectors.signed_webhook.routes import signature

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_the_webhook_example_signs_like_the_server() -> None:
    sign = runpy.run_path(str(EXAMPLES / "signed_webhook.py"))["sign"]
    assert sign("k", "1700000000", b"{}") == signature("k", "1700000000", b"{}")


def test_push_example_sends_valid_payloads() -> None:
    from checkcord.schemas.push import CustomPushPayload

    events = runpy.run_path(str(EXAMPLES / "push_events.py"))["events"]
    reader = "00000000-0000-0000-0000-000000000001"
    public, private = (CustomPushPayload.model_validate(e) for e in events([reader]))
    assert public.access_control and public.access_control.is_public
    assert private.access_control and [
        str(i) for i in private.access_control.allowed_identity_ids
    ] == [reader]


@pytest.mark.parametrize("script", sorted(p.name for p in EXAMPLES.glob("*.py")))
def test_each_example_explains_its_settings(script: str) -> None:
    done = subprocess.run(
        [sys.executable, str(EXAMPLES / script), *(["x"] if script == "mcp_read.py" else [])],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert done.returncode == 2 and "Set CHECKCORD_" in done.stderr

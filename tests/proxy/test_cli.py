import asyncio
import os
import typing
from unittest.mock import patch

from checkcord.cli import main
from checkcord.cli.proxy import print_mode_guidance
from checkcord.proxy.config import ProxySettings
from checkcord.storage.sqlite import SQLiteTelemetryStore


def test_cli_metrics_output(tmp_path: typing.Any, capsys: typing.Any) -> None:
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        # Create .context directory
        os.makedirs(".context", exist_ok=True)
        db_path = ".context/telemetry.db"

        async def setup_db() -> None:
            store = SQLiteTelemetryStore(db_path)
            await store.init_db()
            # Log some dummy requests
            await store.log_request(
                provider="openai",
                model="gpt-4o",
                prompt_tokens=1000,
                completion_tokens=200,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=400,
            )
            await store.log_request(
                provider="anthropic",
                model="claude-3-5-sonnet",
                prompt_tokens=2000,
                completion_tokens=400,
                cache_creation_input_tokens=500,
                cache_read_input_tokens=1000,
            )

        asyncio.run(setup_db())

        # Run CLI metrics command by mocking sys.argv
        with patch("sys.argv", ["checkcord", "proxy", "metrics"]):
            main()

        captured = capsys.readouterr()
        assert "Checkcord Telemetry Dashboard" in captured.out
        assert "Total Requests          | 2" in captured.out
        assert "Cache Hit Rate          | 46.7 %" in captured.out
        assert "Prompt Tokens           | 3,000" in captured.out
        assert "- Cache Read (Hit)      | 1,400" in captured.out
        assert "- Cache Creation (Write)| 500" in captured.out
        assert "Completion Tokens       | 600" in captured.out

    finally:
        os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# Mode-aware startup guidance.
# ---------------------------------------------------------------------------


def test_print_mode_guidance_base_url_mode_default(capsys: typing.Any) -> None:
    """Default flipped to 'base_url' (2026-07-30): regulated-org endpoint
    policy commonly prohibits an unmanaged root CA, so this proves the
    no-argument default now matches that mode without relying on an
    explicit proxy_mode= override.
    """
    settings = ProxySettings(host="127.0.0.1", port=8443, use_tls=True, _env_file=None)
    assert settings.proxy_mode == "base_url"  # new default (was 'mitm')

    print_mode_guidance(settings)
    captured = capsys.readouterr()

    assert "base_url mode" in captured.out
    assert "export ANTHROPIC_BASE_URL=https://127.0.0.1:8443" in captured.out
    assert "export OPENAI_BASE_URL=https://127.0.0.1:8443" in captured.out
    # This mode doesn't ask the developer to trust the CA path.
    assert str(settings.ca_path) not in captured.out


def test_print_mode_guidance_mitm_mode_explicit(capsys: typing.Any) -> None:
    """'mitm' is an explicit opt-in for tools that don't honor a base-URL
    override or that pin certificates; it is not the default, so this sets it.
    """
    settings = ProxySettings(
        proxy_mode="mitm", host="127.0.0.1", port=8443, use_tls=True, _env_file=None
    )

    print_mode_guidance(settings)
    captured = capsys.readouterr()

    assert "mitm mode" in captured.out
    assert str(settings.ca_path) in captured.out
    # No BASE_URL env var instructions in this mode.
    assert "ANTHROPIC_BASE_URL" not in captured.out
    assert "OPENAI_BASE_URL" not in captured.out

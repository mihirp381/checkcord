import json
import os
import tempfile
from pathlib import Path
from typing import Literal

import structlog
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = structlog.get_logger(__name__)


class ProxySettings(BaseSettings):
    """Settings of the local proxy."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_prefix="CHECKCORD_PROXY_", extra="ignore"
    )

    port: int = Field(default=8080, description="Port to listen on")
    host: str = Field(default="127.0.0.1", description="Host address to bind to")
    use_tls: bool = Field(
        default=True, description="Whether to use TLS/HTTPS for the local proxy server"
    )

    openai_upstream: str = Field(
        default="https://api.openai.com", description="Upstream OpenAI API base URL"
    )
    anthropic_upstream: str = Field(
        default="https://api.anthropic.com", description="Upstream Anthropic API base URL"
    )

    # Local certs storage
    cert_dir: str = Field(
        default=".context/certs", description="Directory to store trustme certificates"
    )

    # Database configuration for cross-branch collision detection
    tenant_id: str | None = Field(
        default=None, description="Active tenant UUID for database RLS context"
    )
    database_url: str = Field(
        default="postgresql+asyncpg://checkcord:checkcord@localhost:5432/checkcord",
        description="Postgres database URL for the branch-collision warning",
    )
    api_url: str | None = Field(
        default=None,
        description="Checkcord API base URL; `checkcord mcp` forwards tool calls to its /mcp",
    )
    api_key: SecretStr | None = Field(
        default=None, description="Personal Checkcord API key sent as the Bearer token to api_url"
    )

    # Static environment bypass
    bypass: bool = Field(
        default=False, description="Bypass all proxy parsing and forward request immediately"
    )

    # 'base_url' needs only an env var and the proxy's own localhost
    # certificate, nothing in the system trust store. 'mitm' requires
    # trusting the proxy CA system-wide and is for tools that ignore
    # ANTHROPIC_BASE_URL/OPENAI_BASE_URL.
    proxy_mode: Literal["mitm", "base_url"] = Field(
        default="base_url",
        description=(
            "'base_url' expects the agent's ANTHROPIC_BASE_URL/OPENAI_BASE_URL to "
            "point at this proxy. 'mitm' expects system-wide interception with "
            "the generated CA trusted. The setting changes CLI guidance only; the "
            "proxy behaves the same in both modes."
        ),
    )

    # Web viewer
    web_base_url: str = Field(
        default="http://localhost:3000",
        description=("Base URL of the web viewer, used to build links inside collision warnings."),
    )

    @property
    def cert_path(self) -> Path:
        return Path(self.cert_dir) / "cert.pem"

    @property
    def key_path(self) -> Path:
        return Path(self.cert_dir) / "key.pem"

    @property
    def ca_path(self) -> Path:
        return Path(self.cert_dir) / "ca.pem"


def get_proxy_settings() -> ProxySettings:
    """Load proxy configuration."""
    return ProxySettings()


def get_bypass_config_path() -> Path:
    """Returns the Path to the proxy's dynamic bypass config file.

    Project-relative (under .context/ in the CWD the proxy/CLI is run from),
    matching the rest of the .context/ convention used by cert_dir and the
    telemetry DB in this same module/CLI -- not a global per-user path.
    """
    return Path(".context/config.json")


def write_config(bypass: bool) -> None:
    """Atomically writes the bypass configuration to the dynamic bypass config file."""
    config_path = get_bypass_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    data = {"bypass": bypass}

    # Write atomically using a temporary file in the same directory
    tmp_fd, tmp_path_str = tempfile.mkstemp(dir=str(config_path.parent), suffix=".tmp")
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f)
        # Verify permissions of the config file are secure
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, config_path)
    except Exception as e:
        logger.error("failed_to_write_config_atomically", error=str(e))
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise e


class DynamicBypassCache:
    """The proxy's bypass flag, reread when its file changes."""

    def __init__(self) -> None:
        self._bypass = False
        self._last_mtime = 0.0
        self._last_size = -1

    def get_bypass(self) -> bool:
        config_path = get_bypass_config_path()
        if not config_path.exists():
            return False

        try:
            stat = config_path.stat()
            mtime = stat.st_mtime
            size = stat.st_size

            if mtime != self._last_mtime or size != self._last_size:
                # Reload file on mtime/size mismatch
                with open(config_path) as f:
                    content = json.load(f)
                new_bypass = bool(content.get("bypass", False))
                self._bypass = new_bypass
                self._last_mtime = mtime
                self._last_size = size
                logger.info("reloaded_bypass_config", bypass=new_bypass)
        except json.JSONDecodeError as e:
            logger.warning("invalid_config_json_retaining_cache", error=str(e))
        except Exception as e:
            logger.warning("failed_to_read_config_retaining_cache", error=str(e))

        return self._bypass


_bypass_cache = DynamicBypassCache()


def read_dynamic_bypass() -> bool:
    """Reads dynamic bypass setting from the bypass config file with mtime caching."""
    return _bypass_cache.get_bypass()

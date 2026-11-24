"""Checks that catch a wrong model or embedding width before events fail one by one."""

import argparse
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from langchain_core.embeddings import Embeddings

from checkcord.cli import commands as cli
from checkcord.core.config import Settings
from checkcord.core.startup import StartupConfigError, enforce_embedding_column
from checkcord.pipeline.embedder import EmbeddingClient, EmbeddingDimensionError
from checkcord.services import doctor
from checkcord.storage import vector_column
from checkcord.worker import _probe_embedding_width


class Fixed(Embeddings):
    def __init__(self, width: int) -> None:
        self.width = width

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * self.width for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return [0.1] * self.width


def _settings(**values: Any) -> Settings:
    base: dict[str, Any] = {
        "environment": "development",
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/checkcord",
        "embedding_dimension": 4,
    }
    return Settings(**{**base, **values})


class FakeEngine:
    def connect(self) -> Any:
        conn = MagicMock()
        conn.__aenter__.return_value = conn
        conn.__aexit__.return_value = None
        return conn


def test_mismatch_message() -> None:
    assert vector_column.mismatch(None, 1024) is None
    assert vector_column.mismatch(1024, 1024) is None
    assert "run `checkcord migrate`" in (vector_column.mismatch(1536, 1024) or "")


@pytest.mark.asyncio
async def test_embedder_rejects_the_wrong_width() -> None:
    client = EmbeddingClient(Fixed(3), dimension=4)
    with pytest.raises(EmbeddingDimensionError, match="set EMBEDDING_DIMENSION=3"):
        await client.embed_text("x")
    with pytest.raises(EmbeddingDimensionError):
        await client.embed_texts(["x", "y"])
    assert await EmbeddingClient(Fixed(4), dimension=4).embed_text("x") == [0.1] * 4
    assert await EmbeddingClient(Fixed(3)).embed_text("x") == [0.1] * 3


@pytest.mark.asyncio
async def test_worker_refuses_to_start_on_the_wrong_width() -> None:
    with pytest.raises(StartupConfigError, match="EMBEDDING_DIMENSION"):
        await _probe_embedding_width(EmbeddingClient(Fixed(3), dimension=4))


@pytest.mark.asyncio
async def test_worker_starts_when_the_endpoint_is_down() -> None:
    class Down(Fixed):
        async def aembed_query(self, text: str) -> list[float]:
            raise httpx.ConnectError("refused")

    await _probe_embedding_width(EmbeddingClient(Down(4), dimension=4))


@pytest.mark.asyncio
@pytest.mark.parametrize(("width", "fails"), [(4, False), (None, False), (1024, True)])
async def test_startup_compares_the_column_with_the_setting(
    monkeypatch: pytest.MonkeyPatch, width: int | None, fails: bool
) -> None:
    async def column_dimension(conn: Any, table: str = "") -> int | None:
        return width

    monkeypatch.setattr(vector_column, "column_dimension", column_dimension)
    check = enforce_embedding_column(_settings(), FakeEngine())
    if fails:
        with pytest.raises(StartupConfigError, match="holds 1024-wide vectors"):
            await check
    else:
        await check


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("width", "ok", "detail"),
    [(4, True, "vector(4)"), (8, False, "holds 8-wide"), (None, False, "run `checkcord migrate`")],
)
async def test_doctor_embedding_column(
    monkeypatch: pytest.MonkeyPatch, width: int | None, ok: bool, detail: str
) -> None:
    async def column_dimension(conn: Any, table: str = "") -> int | None:
        return width

    monkeypatch.setattr(vector_column, "column_dimension", column_dimension)
    check = await doctor.embedding_column_check(FakeEngine(), 4)
    assert (check.ok, detail in check.detail) == (ok, True)


def _models(*ids: str) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"data": [{"id": i} for i in ids]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def _local(**values: Any) -> Settings:
    models = {name: "small:7b" for name in doctor_model_fields()}
    return _settings(
        openai_api_key="local-" + "placeholder",
        llm_base_url="http://models.test/v1",
        **{**models, **values},
    )


def doctor_model_fields() -> list[str]:
    from checkcord.llm.client import TASK_MODEL_SETTINGS

    return [
        *TASK_MODEL_SETTINGS.values(),
        "redaction_llm_model",
        "meeting_extraction_llm_model",
        "ci_failure_extraction_llm_model",
    ]


@pytest.mark.asyncio
async def test_doctor_lists_models_on_any_openai_compatible_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "checkcord.pipeline.embeddings_factory.build_embeddings_provider", lambda s: Fixed(4)
    )
    checks = await doctor.model_checks(_local(), _models("small:7b", "other"))
    by_name = {c.name: c for c in checks}
    assert by_name["llm models"].ok
    assert by_name["embedding endpoint"].ok
    assert "4-wide" in by_name["embedding endpoint"].detail


@pytest.mark.asyncio
async def test_doctor_names_missing_models_and_a_wrong_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "checkcord.pipeline.embeddings_factory.build_embeddings_provider", lambda s: Fixed(3)
    )
    checks = await doctor.model_checks(
        _local(linkage_llm_model="judge"), _models("small:7b:latest", "small:7b")
    )
    by_name = {c.name: c for c in checks}
    assert not by_name["llm models"].ok
    assert by_name["llm models"].detail.endswith("does not list judge")
    assert not by_name["embedding endpoint"].ok
    assert "set EMBEDDING_DIMENSION=3" in by_name["embedding endpoint"].detail


@pytest.mark.asyncio
async def test_doctor_reports_an_unreachable_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "checkcord.pipeline.embeddings_factory.build_embeddings_provider", lambda s: None
    )

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    [check] = await doctor.model_checks(_local(), http)
    assert not check.ok and "ConnectError" in check.detail


@pytest.mark.asyncio
async def test_doctor_skips_model_listing_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "checkcord.pipeline.embeddings_factory.build_embeddings_provider", lambda s: None
    )
    assert await doctor.model_checks(_settings(openai_api_key=None)) == []


def test_missing_encryption_key_is_a_warning_in_development() -> None:
    checks = {c.name: c for c in doctor._key_checks(_settings(fernet_master_key=None))}
    assert checks["encryption key"].ok and checks["encryption key"].warn
    assert "never in production" in checks["encryption key"].detail


def _migrate(monkeypatch: pytest.MonkeyPatch, result: Any) -> argparse.Namespace:
    import alembic.command

    monkeypatch.setattr(alembic.command, "upgrade", lambda cfg, rev: None)
    monkeypatch.setattr(cli, "_settings", lambda: _settings())
    monkeypatch.setattr(
        "checkcord.storage.postgres.create_checkcord_engine", lambda url: MagicMock(dispose=_noop)
    )
    calls: list[tuple[int, bool]] = []

    async def sync(engine: Any, dimension: int, *, reset: bool = False) -> str | None:
        calls.append((dimension, reset))
        if isinstance(result, Exception):
            raise result
        return str(result) if result else None

    monkeypatch.setattr(vector_column, "sync", sync)
    args = argparse.Namespace(reset_embeddings=True)
    args.calls = calls
    return args


async def _noop() -> None:
    return None


def test_migrate_sizes_the_column(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _migrate(
        monkeypatch, "resized canonical_contexts.embedding from vector(1024) to vector(4)"
    )
    cli.cmd_migrate(args)
    assert args.calls == [(4, True)]
    assert "to vector(4)" in capsys.readouterr().out


def test_migrate_fails_clearly_when_vectors_are_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    args = _migrate(monkeypatch, vector_column.EmbeddingColumnError("run --reset-embeddings"))
    with pytest.raises(cli.CLIError, match="--reset-embeddings"):
        cli.cmd_migrate(args)


def test_doctor_fails_batch_meeting_judging_off_openai() -> None:
    from checkcord.services.doctor import meeting_judge_check

    assert meeting_judge_check(_settings(meeting_judge_mode="direct")).ok
    local = meeting_judge_check(
        _settings(meeting_judge_mode="batch", llm_base_url="http://localhost:11434/v1")
    )
    assert not local.ok and "MEETING_JUDGE_MODE=direct" in local.detail
    key = "sk-" + "x" * 8
    assert meeting_judge_check(
        _settings(meeting_judge_mode="batch", llm_base_url=None, openai_api_key=key)
    ).ok

import asyncio
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

import checkcord.worker as run_consumers
from checkcord.core.config import Settings
from checkcord.core.startup import StartupConfigError, validate_worker_settings
from checkcord.llm import factory
from checkcord.llm.client import TASK_MODEL_SETTINGS, LLMClient, LLMTask
from checkcord.llm.usage import UsageCallback

BASE_URL = "http://llm-gateway.internal/v1"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "openai_api_key": "sk-from-settings",
        "llm_base_url": BASE_URL,
        "scrubber_llm_model": "m-scrubber",
        "chat_router_model": "m-router",
        "chat_extraction_model": "m-extraction",
        "linkage_llm_model": "m-linkage",
        "skill_synthesis_llm_model": "m-skills",
    }
    values.update(overrides)
    return Settings(**values)


def test_from_settings_builds_one_model_per_task_from_its_setting() -> None:
    settings = _settings()
    client = LLMClient.from_settings(settings)

    assert set(client.task_models) == set(LLMTask)
    for task, field in TASK_MODEL_SETTINGS.items():
        model = client.model_for(task)
        assert isinstance(model, ChatOpenAI)
        assert model.model_name == getattr(settings, field)


def test_from_settings_passes_key_and_base_url_to_every_model() -> None:
    client = LLMClient.from_settings(_settings())

    for model in client.task_models.values():
        assert isinstance(model, ChatOpenAI)
        assert model.openai_api_key is not None
        assert model.openai_api_key.get_secret_value() == "sk-from-settings"
        assert model.openai_api_base == BASE_URL


def test_build_chat_model_passes_settings_through(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_openai = MagicMock()
    monkeypatch.setattr(factory, "ChatOpenAI", chat_openai)

    factory.build_chat_model("m-x", purpose="redaction", temperature=0.3, settings=_settings())

    chat_openai.assert_called_once_with(
        model_name="m-x",
        temperature=0.3,
        max_retries=5,
        openai_api_key="sk-from-settings",
        openai_api_base=BASE_URL,
        request_timeout=60.0,
        callbacks=ANY,
    )
    (callback,) = chat_openai.call_args.kwargs["callbacks"]
    assert isinstance(callback, UsageCallback)
    assert (callback.purpose, callback.model) == ("redaction", "m-x")


def test_build_chat_model_ignores_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-environ")
    model = factory.build_chat_model("m-x", settings=_settings())
    assert model.openai_api_key is not None
    assert model.openai_api_key.get_secret_value() == "sk-from-settings"


def test_build_openai_client_passes_settings_through() -> None:
    client = factory.build_openai_client(_settings())
    assert client.api_key == "sk-from-settings"
    assert str(client.base_url).rstrip("/") == BASE_URL


def _fake_model(content: str) -> MagicMock:
    model = MagicMock(spec=BaseChatModel)
    model.ainvoke = AsyncMock(return_value=AIMessage(content=content))
    return model


OUTCOMES_JSON = (
    '{"outcomes": [{"kind": "decision", "statement": "Use a cache.", "details": [],'
    ' "messages": [1], "replaces_earlier": false}]}'
)

CALLS = {
    LLMTask.CHAT_SCRUBBER: ("WORK", lambda c: c.classify_chat_message("hi")),
    LLMTask.CHAT_ROUTER: (
        '{"tree_id": "t", "domain_tag": "HYBRID", "summary": "s"}',
        lambda c: c.assign_discourse_tree([], [{"content": "x"}]),
    ),
    LLMTask.CHAT_EXTRACTION: (
        OUTCOMES_JSON,
        lambda c: c.extract_outcomes(
            [{"content": "Use a cache.", "author_name": "Dev", "happened_at": None}]
        ),
    ),
    LLMTask.CHAT_SETTLED: ("SETTLED", lambda c: c.judge_settled("a", "b")),
    LLMTask.LINKAGE: ("SAME", lambda c: c.judge_linkage("a", "b")),
    LLMTask.SKILL_SYNTHESIS: ("---\nname: n\n---\nbody", lambda c: c.synthesize_skills("t")),
    LLMTask.PR_CHECK: ("UNRELATED Nothing in common.", lambda c: c.judge_change("d", "diff")),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("task", list(LLMTask))
async def test_each_call_uses_its_own_task_model(task: LLMTask) -> None:
    content, call = CALLS[task]
    models = {t: _fake_model(content) for t in LLMTask}
    client = LLMClient(task_models=models)

    await call(client)

    for other, model in models.items():
        assert model.ainvoke.await_count == (1 if other == task else 0), other


@pytest.mark.parametrize("environment", ["production", "staging", "development"])
def test_worker_refuses_to_start_without_llm_key(environment: str) -> None:
    settings = Settings(environment=environment, openai_api_key=None, voyage_api_key="v")
    with pytest.raises(StartupConfigError, match="OPENAI_API_KEY"):
        validate_worker_settings(settings)


def test_worker_refuses_to_start_without_voyage_key() -> None:
    settings = Settings(environment="production", openai_api_key="sk", voyage_api_key=None)
    with pytest.raises(StartupConfigError, match="VOYAGE_API_KEY"):
        validate_worker_settings(settings)


def test_worker_check_skipped_in_test_environment() -> None:
    validate_worker_settings(Settings(environment="test", openai_api_key=None))


def test_worker_main_fails_before_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(environment="development", openai_api_key=None, voyage_api_key="v")
    monkeypatch.setattr(run_consumers, "get_settings", lambda: settings)
    redis_from_url = MagicMock()
    monkeypatch.setattr(run_consumers.Redis, "from_url", redis_from_url)

    with pytest.raises(StartupConfigError, match="OPENAI_API_KEY"):
        asyncio.run(run_consumers.main())

    redis_from_url.assert_not_called()


def test_build_chat_model_uses_the_timeout_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_openai = MagicMock()
    monkeypatch.setattr(factory, "ChatOpenAI", chat_openai)

    factory.build_chat_model("m-x", settings=_settings(llm_timeout_seconds=7.5))

    assert chat_openai.call_args.kwargs["request_timeout"] == 7.5

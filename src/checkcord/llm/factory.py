from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI

from checkcord.core.config import Settings, get_settings
from checkcord.llm.usage import UsageCallback


def build_chat_model(
    model_name: str,
    *,
    purpose: str = "other",
    temperature: float = 0.0,
    max_retries: int = 5,
    settings: Settings | None = None,
) -> ChatOpenAI:
    """Build a chat model with the configured API key and base URL; its calls are counted."""
    settings = settings or get_settings()
    return ChatOpenAI(
        model_name=model_name,
        temperature=temperature,
        max_retries=max_retries,
        request_timeout=settings.llm_timeout_seconds,
        openai_api_key=settings.openai_api_key,
        openai_api_base=settings.llm_base_url,
        callbacks=[UsageCallback(purpose, model_name)],
    )


def build_openai_client(settings: Settings | None = None) -> AsyncOpenAI:
    """Build a raw OpenAI client with the configured API key and base URL."""
    settings = settings or get_settings()
    return AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.llm_base_url)

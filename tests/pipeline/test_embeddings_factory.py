import pytest
from langchain_openai import OpenAIEmbeddings
from langchain_voyageai import VoyageAIEmbeddings

from checkcord.core.config import Settings
from checkcord.pipeline.embeddings_factory import (
    _voyage_output_dimension,
    build_embeddings_provider,
)


def test_build_embeddings_provider_voyage() -> None:
    settings = Settings(embedding_provider="voyage", voyage_api_key="voyage-test-key")
    provider = build_embeddings_provider(settings)
    assert isinstance(provider, VoyageAIEmbeddings)


def test_build_embeddings_provider_voyage_missing_key_returns_none() -> None:
    settings = Settings(embedding_provider="voyage", voyage_api_key=None)
    assert build_embeddings_provider(settings) is None


def test_build_embeddings_provider_openai() -> None:
    settings = Settings(
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        openai_api_key="openai-test-key",
    )
    provider = build_embeddings_provider(settings)
    assert isinstance(provider, OpenAIEmbeddings)


def test_build_embeddings_provider_openai_missing_key_returns_none() -> None:
    settings = Settings(embedding_provider="openai", openai_api_key=None)
    assert build_embeddings_provider(settings) is None


def test_build_embeddings_provider_unaffected_by_unrelated_settings() -> None:
    """Passing the full concrete Settings object (with every other field at its
    default) must not change provider construction -- the factory only reads
    the five embedding-related fields."""
    settings = Settings(voyage_api_key="voyage-test-key")
    assert settings.embedding_provider == "voyage"  # post-cutover default
    provider = build_embeddings_provider(settings)
    assert isinstance(provider, VoyageAIEmbeddings)


def test_voyage_output_dimension_valid_values() -> None:
    assert _voyage_output_dimension(256) == 256
    assert _voyage_output_dimension(512) == 512
    assert _voyage_output_dimension(1024) == 1024
    assert _voyage_output_dimension(2048) == 2048


def test_voyage_output_dimension_rejects_invalid_value() -> None:
    with pytest.raises(ValueError, match="not a valid Voyage Matryoshka"):
        _voyage_output_dimension(1536)


def test_openai_embeddings_use_configured_base_url() -> None:
    settings = Settings(
        embedding_provider="openai",
        embedding_model="bge-m3",
        openai_api_key="openai-test-key",
        embedding_base_url="http://embed.internal:8080/v1",
    )
    provider = build_embeddings_provider(settings)
    assert isinstance(provider, OpenAIEmbeddings)
    assert provider.openai_api_base == "http://embed.internal:8080/v1"
    assert provider.check_embedding_ctx_length is False


def test_openai_embeddings_unchanged_without_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    settings = Settings(
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        openai_api_key="openai-test-key",
    )
    provider = build_embeddings_provider(settings)
    assert isinstance(provider, OpenAIEmbeddings)
    assert provider.openai_api_base is None
    assert provider.check_embedding_ctx_length is True


def test_openai_model_defaults_to_an_openai_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    assert Settings(embedding_provider="openai").embedding_model == "text-embedding-3-small"
    assert Settings(embedding_provider="voyage").embedding_model == "voyage-code-3"
    assert Settings(embedding_provider="openai", embedding_model="bge-m3").embedding_model == (
        "bge-m3"
    )


@pytest.mark.parametrize("base_url", [None, "http://gateway.internal/v1"])
def test_openai_text_embedding_3_requests_the_column_size(base_url: str | None) -> None:
    settings = Settings(
        embedding_provider="openai",
        embedding_model="text-embedding-3-large",
        openai_api_key="openai-test-key",
        embedding_base_url=base_url,
    )
    provider = build_embeddings_provider(settings)
    assert isinstance(provider, OpenAIEmbeddings)
    assert provider.dimensions == 1024


def test_other_models_are_not_asked_for_a_size_unless_told_to() -> None:
    kwargs = {
        "embedding_provider": "openai",
        "embedding_model": "bge-m3",
        "openai_api_key": "openai-test-key",
        "embedding_base_url": "http://embed.internal:8080/v1",
    }
    default = build_embeddings_provider(Settings(**kwargs))
    forced = build_embeddings_provider(Settings(**kwargs, embedding_send_dimensions=True))
    assert isinstance(default, OpenAIEmbeddings) and default.dimensions is None
    assert isinstance(forced, OpenAIEmbeddings) and forced.dimensions == 1024


def test_send_dimensions_can_be_turned_off() -> None:
    settings = Settings(
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        openai_api_key="openai-test-key",
        embedding_send_dimensions=False,
    )
    provider = build_embeddings_provider(settings)
    assert isinstance(provider, OpenAIEmbeddings)
    assert provider.dimensions is None


@pytest.mark.parametrize(
    ("provider", "model", "dimension", "fragment"),
    [
        ("openai", "text-embedding-ada-002", 1024, "always returns 1536"),
        ("openai", "azure/text-embedding-ada-002", 1024, "always returns 1536"),
        ("openai", "text-embedding-3-small", 2048, "at most 1536"),
        ("voyage", "voyage-code-3", 1536, "Voyage models return"),
    ],
)
def test_dimension_problem_names_the_mismatch(
    provider: str, model: str, dimension: int, fragment: str
) -> None:
    from checkcord.core.embedding_models import dimension_problem

    problem = dimension_problem(provider, model, dimension)
    assert problem is not None and fragment in problem


@pytest.mark.parametrize(
    ("provider", "model", "dimension"),
    [
        ("openai", "text-embedding-3-small", 1024),
        ("openai", "text-embedding-3-large", 1024),
        ("openai", "text-embedding-ada-002", 1536),
        ("openai", "bge-m3", 1024),
        ("voyage", "voyage-code-3", 1024),
    ],
)
def test_dimension_problem_is_silent_when_the_model_fits(
    provider: str, model: str, dimension: int
) -> None:
    from checkcord.core.embedding_models import dimension_problem

    assert dimension_problem(provider, model, dimension) is None

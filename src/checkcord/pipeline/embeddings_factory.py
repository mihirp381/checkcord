from __future__ import annotations

from typing import Literal, Protocol

import structlog
from langchain_core.embeddings import Embeddings
from pydantic import SecretStr

from checkcord.core.embedding_models import sends_dimensions

logger = structlog.get_logger(__name__)


def _voyage_output_dimension(dimension: int) -> Literal[256, 512, 1024, 2048]:
    if dimension == 256:
        return 256
    if dimension == 512:
        return 512
    if dimension == 1024:
        return 1024
    if dimension == 2048:
        return 2048
    raise ValueError(
        f"embedding_dimension={dimension} is not a valid Voyage Matryoshka "
        "truncation point; must be one of 256, 512, 1024, 2048."
    )


class EmbeddingSettings(Protocol):
    """The embedding fields of Settings, as read-only properties so its Literal types fit."""

    @property
    def embedding_provider(self) -> str: ...
    @property
    def embedding_model(self) -> str: ...
    @property
    def embedding_dimension(self) -> int: ...
    @property
    def voyage_api_key(self) -> str | None: ...
    @property
    def openai_api_key(self) -> str | None: ...
    @property
    def embedding_base_url(self) -> str | None: ...
    @property
    def embedding_send_dimensions(self) -> bool | None: ...


def build_embeddings_provider(settings: EmbeddingSettings) -> Embeddings | None:
    """Construct the configured LangChain Embeddings backend, or None if unconfigured.

    Used by both ingestion and query so the two sides share one client stack.
    """
    if settings.embedding_provider == "voyage":
        if not settings.voyage_api_key:
            return None
        from langchain_voyageai import VoyageAIEmbeddings

        logger.info(
            "embeddings.voyage_init",
            model=settings.embedding_model,
            output_dimension=settings.embedding_dimension,
        )
        return VoyageAIEmbeddings(
            model=settings.embedding_model,
            voyage_api_key=SecretStr(settings.voyage_api_key),
            output_dimension=_voyage_output_dimension(settings.embedding_dimension),
        )

    if not settings.openai_api_key:
        return None
    from langchain_openai import OpenAIEmbeddings

    dimensions = (
        settings.embedding_dimension
        if sends_dimensions(settings.embedding_model, settings.embedding_send_dimensions)
        else None
    )
    logger.info(
        "embeddings.openai_init",
        model=settings.embedding_model,
        base_url=settings.embedding_base_url,
        dimensions=dimensions,
    )
    if settings.embedding_base_url:
        # Compatible servers expect plain strings, not tiktoken token arrays.
        return OpenAIEmbeddings(
            model=settings.embedding_model,
            openai_api_key=SecretStr(settings.openai_api_key),
            openai_api_base=settings.embedding_base_url,
            check_embedding_ctx_length=False,
            dimensions=dimensions,
        )
    return OpenAIEmbeddings(
        model=settings.embedding_model,
        openai_api_key=SecretStr(settings.openai_api_key),
        dimensions=dimensions,
    )

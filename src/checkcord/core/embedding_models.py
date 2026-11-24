"""Embedding model defaults and vector-size rules."""

DEFAULT_EMBEDDING_MODELS = {"voyage": "voyage-code-3", "openai": "text-embedding-3-small"}
VOYAGE_DIMENSIONS = (256, 512, 1024, 2048)
_OPENAI_MAX_DIMENSIONS = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}
_OPENAI_FIXED_DIMENSIONS = {"text-embedding-ada-002": 1536}


def _base_name(model: str) -> str:
    return model.rsplit("/", 1)[-1]


def sends_dimensions(model: str, override: bool | None) -> bool:
    """Whether an OpenAI-style request should ask for a specific vector size."""
    if override is not None:
        return override
    return _base_name(model) in _OPENAI_MAX_DIMENSIONS


def dimension_problem(provider: str, model: str, dimension: int) -> str | None:
    """Why ``model`` cannot produce ``dimension``-wide vectors, or None if no problem is known."""
    if provider == "voyage":
        if dimension not in VOYAGE_DIMENSIONS:
            sizes = ", ".join(str(size) for size in VOYAGE_DIMENSIONS)
            return f"Voyage models return {sizes}-wide vectors; EMBEDDING_DIMENSION is {dimension}."
        return None
    name = _base_name(model)
    fixed = _OPENAI_FIXED_DIMENSIONS.get(name)
    if fixed is not None and fixed != dimension:
        return (
            f"{name} always returns {fixed}-wide vectors but EMBEDDING_DIMENSION is {dimension}; "
            "use text-embedding-3-small or text-embedding-3-large."
        )
    largest = _OPENAI_MAX_DIMENSIONS.get(name)
    if largest is not None and dimension > largest:
        return f"{name} returns at most {largest}-wide vectors; EMBEDDING_DIMENSION is {dimension}."
    return None

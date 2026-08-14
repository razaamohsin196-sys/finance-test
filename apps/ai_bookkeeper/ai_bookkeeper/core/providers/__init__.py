"""AI provider adapters."""

from __future__ import annotations

from .base import (
    PROMPT_VERSION,
    RESPONSE_SCHEMA,
    CategorizationProvider,
    ProviderError,
    build_system_prompt,
    build_user_prompt,
)
from .mock import MockProvider

__all__ = [
    "PROMPT_VERSION",
    "RESPONSE_SCHEMA",
    "CategorizationProvider",
    "MockProvider",
    "ProviderError",
    "build_provider",
    "build_system_prompt",
    "build_user_prompt",
]


def build_provider(name: str, *, api_key: str | None = None, model: str | None = None,
                   effort: str = "low") -> CategorizationProvider:
    """Resolve a provider by name.

    ``anthropic_provider`` is imported lazily so a bench without the SDK
    installed can still run the mock path.
    """
    key = (name or "mock").strip().lower()

    if key == "mock":
        return MockProvider()

    if key == "anthropic":
        from .anthropic_provider import DEFAULT_MODEL, AnthropicProvider

        return AnthropicProvider(
            api_key=api_key or "",
            model=model or DEFAULT_MODEL,
            effort=effort,
        )

    raise ProviderError(
        f"Unknown AI provider {name!r}. Supported: 'mock', 'anthropic'."
    )

"""Anthropic Claude backend.

Uses structured outputs (``output_config.format``) so the model is constrained
to the response schema in ``base.RESPONSE_SCHEMA`` -- including the account
enum. That removes a whole class of failure: there is no prose to strip, no
JSON to salvage with a regex, and an account name outside the chart of accounts
cannot come back in the first place.

The key is read from the caller and never from a literal. ``ai_bookkeeper``
passes it in from ``site_config.json``.
"""

from __future__ import annotations

import json

from ..models import ProviderResult, RawTransaction
from .base import (
    RESPONSE_SCHEMA,
    CategorizationProvider,
    ProviderError,
    build_system_prompt,
    build_user_prompt,
)

DEFAULT_MODEL = "claude-opus-5"


class AnthropicProvider(CategorizationProvider):
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        effort: str = "low",
        max_tokens: int = 4000,
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise ProviderError(
                "No Anthropic API key configured. Set anthropic_api_key in "
                "site_config.json, or switch AI Bookkeeping Settings to the "
                "'mock' provider to run offline."
            )
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ProviderError(
                "The 'anthropic' package is not installed in this bench. "
                "Run: bench pip install anthropic"
            ) from exc

        self.model = model
        self._effort = effort
        self._max_tokens = max_tokens
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
        self._errors = anthropic

    def categorize(self, txn: RawTransaction) -> ProviderResult:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                system=build_system_prompt(),
                messages=[{"role": "user", "content": build_user_prompt(txn)}],
                output_config={
                    "effort": self._effort,
                    "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
                },
            )
        except self._errors.APIError as exc:
            # Surfaced as ProviderError so the pipeline routes the row to
            # review rather than dropping it or, worse, posting a guess.
            raise ProviderError(f"Anthropic API call failed: {exc}") from exc

        if response.stop_reason == "refusal":
            raise ProviderError(
                "Model declined to answer for this transaction "
                f"(category: {getattr(response.stop_details, 'category', None)})."
            )

        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise ProviderError(
                f"No text block in response (stop_reason={response.stop_reason})."
            )

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Response was not valid JSON: {text[:200]!r}") from exc

        return self._coerce(payload, raw=text)

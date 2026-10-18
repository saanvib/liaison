"""Injectable LLM client for the Guard's judge and grounding check (A9).

Tests inject fakes that implement `LLMClient`; nothing here touches the network
unless `AnthropicLLM.complete` is called. The `anthropic` package is imported
lazily, so the rules, the eval and the offline path never need it, and a
missing package surfaces as an LLMError (which the judge turns into
needs_review, never approved).
"""
from __future__ import annotations

import os
from typing import Optional, Protocol

DEFAULT_MODEL = "claude-sonnet-5-5"
# The Vendor Agent waits 10 s for a verdict (D4) and a draft check can make two
# LLM calls (judge, then grounding), so each call gets well under half of that.
DEFAULT_TIMEOUT_S = 4.0


class LLMError(Exception):
    """Any failure to get a usable completion: network, timeout, refusal, config."""


class LLMClient(Protocol):
    def complete(self, system: str, prompt: str, schema: Optional[dict] = None) -> str:
        """Return the model's text reply. With `schema`, the reply is JSON matching it."""
        ...


class AnthropicLLM:
    """Claude via the official SDK. Configured by LLM_API_KEY, LLM_MODEL, GUARD_LLM_TIMEOUT_S."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ):
        self.api_key = api_key or os.environ.get("LLM_API_KEY")
        self.model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
        self.timeout_s = timeout_s or float(os.environ.get("GUARD_LLM_TIMEOUT_S", DEFAULT_TIMEOUT_S))
        self._client = None

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise LLMError("LLM_API_KEY is not set")
            try:
                import anthropic
            except ImportError as e:
                raise LLMError("the 'anthropic' package is not installed") from e
            # No retries: a retry would blow the Guard's time budget; the caller fails closed.
            self._client = anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout_s, max_retries=0)
        return self._client

    def complete(self, system: str, prompt: str, schema: Optional[dict] = None) -> str:
        client = self._get_client()
        output_config: dict = {"effort": "low"}  # short classification; low effort keeps latency down
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        try:
            response = client.beta.messages.create(
                model=self.model,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config=output_config,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except Exception as e:  # SDK errors, timeouts, connection failures: all fail closed upstream
            raise LLMError(f"{type(e).__name__}: {e}") from e
        if response.stop_reason != "end_turn":
            raise LLMError(f"unusable completion (stop_reason={response.stop_reason})")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise LLMError("completion had no text")
        return text

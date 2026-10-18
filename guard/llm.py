"""Injectable LLM clients for the Guard's judge and grounding check (A9).

Tests inject fakes that implement `LLMClient`; nothing here touches the network
unless a client's `complete` is called. `make_llm()` picks the provider from
LLM_MODEL: `gemini-*` uses `GeminiLLM` (REST over the standard library, so the
hosted agent needs no extra package), anything else `AnthropicLLM` (the
`anthropic` package, imported lazily). Every failure surfaces as an LLMError,
which the judge turns into needs_review, never approved.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
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
    """Claude via the official SDK. Configured by LLM_API_KEY, LLM_MODEL, GUARD_LLM_TIMEOUT_S, and
    LLM_WORKSPACE_ID (required when the key isn't scoped to one workspace; sent as anthropic-workspace-id)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_s: Optional[float] = None,
        workspace_id: Optional[str] = None,
    ):
        self.api_key = api_key or os.environ.get("LLM_API_KEY")
        self.model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
        self.timeout_s = timeout_s or float(os.environ.get("GUARD_LLM_TIMEOUT_S", DEFAULT_TIMEOUT_S))
        self.workspace_id = workspace_id or os.environ.get("LLM_WORKSPACE_ID") or None
        self._client = None

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise LLMError("LLM_API_KEY is not set")
            try:
                import anthropic
            except ImportError as e:
                raise LLMError("the 'anthropic' package is not installed") from e
            headers = {"anthropic-workspace-id": self.workspace_id} if self.workspace_id else None
            # No retries: a retry would blow the Guard's time budget; the caller fails closed.
            self._client = anthropic.Anthropic(
                api_key=self.api_key, timeout=self.timeout_s, max_retries=0, default_headers=headers
            )
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


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _ssl_context() -> ssl.SSLContext:
    """certifi's CA bundle when installed (python.org macOS builds ship none), else the system's."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


class GeminiLLM:
    """Gemini via its REST API (generateContent). Configured by LLM_API_KEY, LLM_MODEL and
    GUARD_LLM_TIMEOUT_S. The key goes in the x-goog-api-key header, never the URL."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_s: Optional[float] = None,
        url: str = GEMINI_URL,
    ):
        self.api_key = api_key or os.environ.get("LLM_API_KEY")
        self.model = model or os.environ.get("LLM_MODEL") or "gemini-3.5-flash"
        self.timeout_s = timeout_s or float(os.environ.get("GUARD_LLM_TIMEOUT_S", DEFAULT_TIMEOUT_S))
        self.url = url

    def complete(self, system: str, prompt: str, schema: Optional[dict] = None) -> str:
        if not self.api_key:
            raise LLMError("LLM_API_KEY is not set")
        generation: dict = {"maxOutputTokens": 1024}
        if schema is not None:
            generation.update(responseMimeType="application/json", responseJsonSchema=schema)
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation,
        }
        req = urllib.request.Request(
            self.url.format(model=self.model),
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s, context=_ssl_context()) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            try:
                detail = json.loads(detail)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                pass
            raise LLMError(f"HTTP {e.code}: {detail[:200]}") from e
        except Exception as e:  # timeouts, connection and TLS failures: all fail closed upstream
            raise LLMError(f"{type(e).__name__}: {e}") from e
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise LLMError(f"unusable completion ({reason})")
        first = candidates[0]
        if first.get("finishReason") != "STOP":
            raise LLMError(f"unusable completion (finishReason={first.get('finishReason')})")
        text = "".join(part.get("text", "") for part in (first.get("content") or {}).get("parts", []))
        if not text:
            raise LLMError("completion had no text")
        return text


def make_llm() -> LLMClient:
    """The client LLM_MODEL names: GeminiLLM for gemini-*, else AnthropicLLM (default claude-sonnet-5-5)."""
    model = os.environ.get("LLM_MODEL") or DEFAULT_MODEL
    return GeminiLLM(model=model) if model.lower().startswith("gemini") else AnthropicLLM(model=model)

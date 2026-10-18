"""Injectable clients: Guard, Router, LLM, Embedder (A9). Tests inject fakes;
nothing here touches the network unless the real methods below are called.

HttpEmbedder follows the same wire format experts/embed.py already settled on
(OpenAI- and Voyage-compatible): POST {"model": ..., "input": [texts]} with a
Bearer key, response {"data": [{"embedding": [...]}, ...]} in input order.

LLMClient and GeminiEmbedder call Gemini's REST API directly over the standard
library, not the google-genai SDK: guard/llm.py's hosted-Agentverse probe found
only a fixed set of packages available there (anthropic, uagents, pydantic...),
not google-genai, so an SDK-based client would fail to import hosted.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import ssl
import urllib.error
import urllib.request
from typing import Optional, Protocol, Sequence

from contracts import Escalation, GuardRequest, GuardVerdict, Topic

Vector = list[float]

GEMINI_GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_EMBED_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"


def _ssl_context() -> ssl.SSLContext:
    """certifi's CA bundle when installed (python.org macOS builds ship none), else the system's."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _gemini_post(url: str, api_key: str, body: dict, timeout_s: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=_ssl_context()) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            pass
        raise LLMError(f"HTTP {e.code}: {detail[:200]}") from e
    except Exception as e:  # timeouts, connection and TLS failures: all fail closed upstream
        raise LLMError(f"{type(e).__name__}: {e}") from e


class GuardClient(Protocol):
    async def check(self, req: GuardRequest, timeout_s: float = 10.0) -> Optional[GuardVerdict]: ...


class RouterClient(Protocol):
    async def send_escalation(self, esc: Escalation) -> None: ...
    async def reply_chat(self, to_sender: str, text: str) -> None: ...


class LLMError(Exception):
    """Any failure to get a usable completion: network, timeout, refusal, config."""


class LLMClient:
    """Gemini via its REST API (generateContent, same shape as guard/llm.py's
    GeminiLLM), for drafting answers and classifying topics."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, timeout_s: float = 8.0):
        self.api_key = api_key or os.environ.get("LLM_API_KEY")
        self.model = model or os.environ.get("LLM_MODEL") or "gemini-3.6-flash"
        self.timeout_s = timeout_s

    def _complete(self, system: str, prompt: str) -> str:
        if not self.api_key:
            raise LLMError("LLM_API_KEY is not set")
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 1024},
        }
        data = _gemini_post(GEMINI_GENERATE_URL.format(model=self.model), self.api_key, body, self.timeout_s)
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise LLMError(f"unusable completion ({reason})")
        first = candidates[0]
        if first.get("finishReason") != "STOP":
            raise LLMError(f"unusable completion (finishReason={first.get('finishReason')})")
        text = "".join(p.get("text", "") for p in first.get("content", {}).get("parts", []))
        if not text:
            raise LLMError("completion had no text")
        return text

    def draft(self, question: str, hits: Sequence) -> str:
        context = "\n\n".join(f"[{h.doc_title}] {h.text}" for h in hits)
        system = (
            "You answer customer questions about our product using only the provided excerpts. "
            "Cite facts only from the excerpts; do not invent anything not present in them."
        )
        prompt = f"Excerpts:\n{context}\n\nQuestion: {question}\n\nAnswer:"
        return self._complete(system, prompt)

    def classify_topic(self, question: str) -> Topic:
        values = [t.value for t in Topic]
        system = f"Classify the question into exactly one of: {', '.join(values)}. Reply with only that word."
        reply = self._complete(system, question).strip().lower()
        for t in Topic:
            if t.value == reply:
                return t
        return Topic.RND


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[Vector]: ...


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


_TOKEN = re.compile(r"[a-z0-9]+")


class OfflineEmbedder:
    """Offline fallback: binary bag of words, hashed into `dims` buckets. Used
    when EMBEDDINGS_URL is unset so dev and tests run without a network."""

    def __init__(self, dims: int = 4096):
        self.dims = dims

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        out = []
        for text in texts:
            vec = [0.0] * self.dims
            for tok in _TOKEN.findall(text.lower()):
                vec[int(hashlib.md5(tok.encode()).hexdigest(), 16) % self.dims] = 1.0
            out.append(vec)
        return out


class HttpEmbedder:
    def __init__(self, url: str, api_key: Optional[str] = None, model: Optional[str] = None, timeout_s: float = 10.0):
        self.url, self.api_key, self.model, self.timeout_s = url, api_key, model, timeout_s

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        body: dict = {"input": list(texts)}
        if self.model:
            body["model"] = self.model
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            data = json.load(resp)["data"]
        return [item["embedding"] for item in data]


class GeminiEmbedder:
    """Embeddings via Gemini's REST API (batchEmbedContents), same account/key
    as LLMClient, no SDK dependency (see module docstring)."""

    def __init__(self, api_key: str, model: Optional[str] = None, timeout_s: float = 10.0):
        self.api_key = api_key
        self.model = model or "gemini-embedding-001"
        self.timeout_s = timeout_s

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        body = {
            "requests": [
                {"model": f"models/{self.model}", "content": {"parts": [{"text": t}]}} for t in texts
            ]
        }
        data = _gemini_post(GEMINI_EMBED_URL.format(model=self.model), self.api_key, body, self.timeout_s)
        return [e["values"] for e in data["embeddings"]]


def default_embedder(config) -> Embedder:
    if config.embeddings_url:
        return HttpEmbedder(config.embeddings_url, config.embeddings_api_key, config.embeddings_model)
    api_key = config.embeddings_api_key or config.llm_api_key
    if api_key:
        return GeminiEmbedder(api_key, config.embeddings_model)
    return OfflineEmbedder()

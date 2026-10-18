"""Text embedders for expert matching (A9).

`default_embedder()` returns `HttpEmbedder` when EMBEDDINGS_URL is set, else
`BagOfWordsEmbedder`, so dev and tests run without a network.

HttpEmbedder assumes the common embeddings API shape (OpenAI- and
Voyage-compatible): POST {"model": ..., "input": [texts]} with a Bearer key,
response {"data": [{"embedding": [...]}, ...]} in input order.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.request
from typing import Optional, Protocol, Sequence

Vector = list[float]


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[Vector]: ...


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


_STOPWORDS = frozenset(
    """a an the and or but if of to in on at by for with from into onto as is are was were be been being
    it its this that these those i you we they he she my your our their me us them what which who whom
    how when where why can could should would will may might must do does did done have has had not no
    so than then there here about over under after before up down out any all some each per via also
    get got please hi hello thanks thank need want know tell ask
    use using used show shows showing really just still very won wont isn don doesn didn like make
    help work works going way thing things anyone someone""".split()
)
_TOKEN = re.compile(r"[a-z0-9]+")


def _stem(token: str) -> str:
    """Crude suffix strip so plurals and verb forms meet: codes/code, samples/sample, shipped/ship."""
    for suffix in ("ing", "ies", "ed", "s"):
        if token.endswith(suffix) and not token.endswith("ss") and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return token


def tokens(text: str) -> set[str]:
    return {_stem(t) for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1}


class BagOfWordsEmbedder:
    """Offline fallback: binary bag of stemmed content words, hashed into `dims` buckets.

    Cosine between two such vectors is |shared words| / sqrt(|words a| * |words b|).
    """

    def __init__(self, dims: int = 4096):
        self.dims = dims

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        out = []
        for text in texts:
            vec = [0.0] * self.dims
            for tok in tokens(text):
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


def default_embedder() -> Embedder:
    url = os.environ.get("EMBEDDINGS_URL")
    if not url:
        return BagOfWordsEmbedder()
    return HttpEmbedder(url, os.environ.get("EMBEDDINGS_API_KEY"), os.environ.get("EMBEDDINGS_MODEL"))

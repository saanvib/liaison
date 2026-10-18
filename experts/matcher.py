"""Expert matching (D10, D11, D23): name the person best placed to answer a question.

Each expert's role and summary lines are embedded as separate chunks; an expert
scores as their best chunk's cosine similarity to the question. Below
`threshold` the result is a fallback ("use the topic channel").

Evidence chunks (GitHub commits and PRs, from `python -m experts.ingest_github`)
are loaded from evidence.json when it exists and scored like summary lines,
scaled by `evidence_weight` because a commit is weaker evidence than a profile.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from contracts import Topic

from experts.embed import Embedder, cosine, default_embedder, tokens

DIRECTORY_PATH = Path(__file__).with_name("directory.yaml")
EVIDENCE_PATH = Path(__file__).with_name("evidence.json")
DEFAULT_THRESHOLD = 0.35
DEFAULT_EVIDENCE_WEIGHT = 0.9
# Topic when nothing matches and no keyword fires: application scientists take general product questions.
DEFAULT_TOPIC = Topic.RND

# Small keyword classifier, used only when no expert clears the threshold and no topic was given.
TOPIC_KEYWORDS: dict[Topic, frozenset[str]] = {
    Topic.RND: frozenset(tokens(
        "assay protocol extraction yield lysis elution buffer sample samples incubation sensitivity "
        "detection pcr sequencing ct amplification contamination plasma serum dna rna reagent wash"
    )),
    Topic.QA_RA: frozenset(tokens(
        "regulatory ruo ivd fda ce marking label labeling ifu claim compliance certificate coa iso "
        "audit lot release complaint recall capa stability shelf expiry reach rohs quality"
    )),
    Topic.ENGINEERING: frozenset(tokens(
        "analyzer instrument hardware firmware software error code sensor calibration maintenance "
        "install installation lims export csv api barcode update windows centrifuge heater motor"
    )),
    Topic.SALES_OPS: frozenset(tokens(
        "price pricing quote discount contract order purchase invoice payment shipping shipment "
        "delivery dry ice customs stock inventory backorder lead time distributor"
    )),
}


@dataclass
class Expert:
    id: str
    slack_user_id: str
    display_name: str
    role: str
    topics: list[Topic]
    summary: str


@dataclass
class MatchResult:
    expert: Optional[Expert]
    topic: Topic
    score: float
    fallback: bool  # True: no expert cleared the threshold; use the topic channel


def load_directory(path: Path = DIRECTORY_PATH) -> list[Expert]:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)["experts"]
    experts = [
        Expert(
            id=e["id"],
            slack_user_id=e["slack_user_id"],
            display_name=e["display_name"],
            role=e["role"],
            topics=[Topic(t) for t in e["topics"]],
            summary=e["summary"].strip(),
        )
        for e in raw
    ]
    for field in ("id", "slack_user_id"):
        values = [getattr(e, field) for e in experts]
        dupes = sorted({v for v in values if values.count(v) > 1})
        if dupes:
            raise ValueError(f"duplicate {field} in {path.name}: {', '.join(dupes)}")
    for e in experts:
        if not e.topics:
            raise ValueError(f"expert {e.id} has no topics")
    return experts


_LABEL = re.compile(r"^\s*(?:project|meeting notes?)\s*:\s*", re.IGNORECASE)


def chunks(expert: Expert) -> list[str]:
    """Role plus each summary line, without 'Project:' / 'Meeting notes:' labels (shared filler)."""
    lines = [expert.role] + [_LABEL.sub("", ln).strip() for ln in re.split(r"\n+", expert.summary)]
    return [ln for ln in lines if ln]


def load_evidence(path: Path = EVIDENCE_PATH) -> dict[str, list[str]]:
    """Evidence texts per expert id; empty when the file is absent (no ingest has run)."""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)["chunks"]
    out: dict[str, list[str]] = {}
    for c in raw:
        out.setdefault(c["expert_id"], []).append(c["text"])
    return out


def classify_topic(question: str) -> Optional[Topic]:
    """Keyword vote; None when no keyword fires or the top topics tie."""
    words = tokens(question)
    scores = {topic: len(words & kw) for topic, kw in TOPIC_KEYWORDS.items()}
    best = max(scores.values())
    winners = [t for t, s in scores.items() if s == best]
    return winners[0] if best > 0 and len(winners) == 1 else None


class Matcher:
    def __init__(
        self,
        experts: list[Expert],
        embedder: Embedder,
        threshold: float = DEFAULT_THRESHOLD,
        evidence: Optional[dict[str, list[str]]] = None,
        evidence_weight: float = DEFAULT_EVIDENCE_WEIGHT,
    ):
        self.experts = experts
        self.embedder = embedder
        self.threshold = threshold
        self.evidence = evidence or {}
        self.evidence_weight = evidence_weight
        self._chunk_vectors: Optional[list[list[tuple[float, list[float]]]]] = None

    def _vectors(self) -> list[list[tuple[float, list[float]]]]:
        """Per expert, (weight, vector) for each profile chunk then each evidence chunk."""
        if self._chunk_vectors is None:
            per_expert = [
                [(1.0, c) for c in chunks(e)] + [(self.evidence_weight, t) for t in self.evidence.get(e.id, [])]
                for e in self.experts
            ]
            flat = self.embedder.embed([t for cs in per_expert for _, t in cs])
            out, i = [], 0
            for cs in per_expert:
                out.append([(w, v) for (w, _), v in zip(cs, flat[i : i + len(cs)])])
                i += len(cs)
            self._chunk_vectors = out
        return self._chunk_vectors

    def match(self, question: str, topic: Optional[Topic] = None) -> MatchResult:
        q = self.embedder.embed([question])[0]
        best_expert, best_score = None, 0.0
        for expert, vectors in zip(self.experts, self._vectors()):
            if topic is not None and topic not in expert.topics:
                continue
            score = max((w * cosine(q, v) for w, v in vectors), default=0.0)
            if score > best_score:
                best_expert, best_score = expert, score

        if best_expert is not None and best_score >= self.threshold:
            return MatchResult(best_expert, topic or self._topic_for(best_expert, question), best_score, fallback=False)
        inferred = topic or classify_topic(question) or DEFAULT_TOPIC
        return MatchResult(None, inferred, best_score, fallback=True)

    @staticmethod
    def _topic_for(expert: Expert, question: str) -> Topic:
        """The expert's topic the question is about (keyword vote), else their first topic."""
        if len(expert.topics) > 1:
            voted = classify_topic(question)
            if voted in expert.topics:
                return voted
        return expert.topics[0]

    def allowlist(self, topic: Topic) -> set[str]:
        return {e.slack_user_id for e in self.experts if topic in e.topics}


_default: Optional[Matcher] = None


def default_matcher() -> Matcher:
    global _default
    if _default is None:
        threshold = float(os.environ.get("EXPERT_MATCH_THRESHOLD", DEFAULT_THRESHOLD))
        _default = Matcher(load_directory(), default_embedder(), threshold, load_evidence(EVIDENCE_PATH))
    return _default

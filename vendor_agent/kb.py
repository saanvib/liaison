"""KnowledgeBase: KB lookup and the strict write/accept trust rule (D14, D18, D19, D23, T10)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from contracts import ApprovedAnswer, Escalation, Topic, Verdict

from vendor_agent.clients import Embedder, cosine
from vendor_agent.models import AcceptResult
from vendor_agent.retrieval import Storage

logger = logging.getLogger(__name__)

_PENDING_KEY = "vendor_agent:kb_pending"
_ENTRIES_KEY = "vendor_agent:kb_entries"
_WRITTEN_KEY = "vendor_agent:kb_written_escalations"


@dataclass
class PendingEscalation:
    escalation_id: str
    flagged: bool
    question: str
    asker_ref: Optional[str]
    created_at: str


@dataclass
class KBEntry:
    question: str
    answer: str
    topic: Topic
    approver_id: str
    approver_display_name: str
    date: str  # YYYY-MM-DD, the day it was written
    created_at: str
    vector: list[float]


def _parse_approved_by(approved_by: str) -> tuple[Optional[str], Optional[str]]:
    """"<slack_user_id>|<display name>" (A4). Returns (None, None) if malformed."""
    if "|" not in approved_by:
        return None, None
    approver_id, _, display_name = approved_by.partition("|")
    approver_id, display_name = approver_id.strip(), display_name.strip()
    if not approver_id or not display_name:
        return None, None
    return approver_id, display_name


class KnowledgeBase:
    def __init__(
        self,
        storage: Storage,
        embedder: Embedder,
        config,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.storage = storage
        self.embedder = embedder
        self.config = config
        self.clock = clock

    # -- pending escalations (A7) --

    def add_pending(self, escalation: Escalation, flagged: bool) -> None:
        pending = self.storage.get(_PENDING_KEY) or {}
        pending[escalation.escalation_id] = {
            "escalation_id": escalation.escalation_id,
            "flagged": flagged,
            "question": escalation.question,
            "asker_ref": escalation.asker_ref,
            "created_at": self.clock().isoformat(),
        }
        self.storage.set(_PENDING_KEY, pending)

    def get_pending(self, escalation_id: str) -> Optional[PendingEscalation]:
        raw = (self.storage.get(_PENDING_KEY) or {}).get(escalation_id)
        return None if raw is None else PendingEscalation(**raw)

    # -- lookup --

    def lookup(self, question: str, vector: Optional[list[float]] = None) -> Optional[KBEntry]:
        entries = self.storage.get(_ENTRIES_KEY) or []
        if not entries:
            return None
        query_vector = vector if vector is not None else self.embedder.embed([question])[0]
        best, best_score = None, -1.0
        for e in entries:
            score = cosine(query_vector, e["vector"])
            if score > best_score:
                best, best_score = e, score
        if best is None or best_score < self.config.kb_match_threshold:
            return None
        return KBEntry(
            question=best["question"],
            answer=best["answer"],
            topic=Topic(best["topic"]),
            approver_id=best["approver_id"],
            approver_display_name=best["approver_display_name"],
            date=best["date"],
            created_at=best["created_at"],
            vector=best["vector"],
        )

    # -- accept (D14, D18, D19, D23, T10) --

    def accept(self, msg: ApprovedAnswer, sender: str) -> AcceptResult:
        result = self._accept(msg, sender)
        logger.info(
            "kb_accept escalation_id=%s accepted=%s reason=%s",
            msg.escalation_id, result.accepted, result.reason,
        )
        return result

    def _accept(self, msg: ApprovedAnswer, sender: str) -> AcceptResult:
        if sender != self.config.router_address:
            return AcceptResult(False, "untrusted_sender")

        pending = self.get_pending(msg.escalation_id)
        if pending is None:
            return AcceptResult(False, "unknown_escalation")

        written = set(self.storage.get(_WRITTEN_KEY) or [])
        if msg.escalation_id in written:
            return AcceptResult(True, "duplicate")

        if pending.flagged:
            return AcceptResult(False, "flagged_not_stored")

        if msg.guard_verdict is not None:
            if msg.guard_verdict.verdict != Verdict.APPROVED:
                return AcceptResult(False, "bad_verdict")
            approver_id, approver_display_name = _parse_approved_by(msg.approved_by)
        else:
            approver_id, approver_display_name = _parse_approved_by(msg.approved_by)
            if approver_id is None or approver_id not in self.config.approver_allowlist:
                return AcceptResult(False, "approver_not_allowed")

        entry = {
            "question": pending.question,
            "answer": msg.answer,
            "topic": msg.topic.value,
            "approver_id": approver_id or "",
            "approver_display_name": approver_display_name or msg.approved_by,
            "date": self.clock().date().isoformat(),
            "created_at": pending.created_at,
            "vector": self.embedder.embed([pending.question])[0],
        }
        try:
            entries = self.storage.get(_ENTRIES_KEY) or []
            entries.append(entry)
            self.storage.set(_ENTRIES_KEY, entries)
            written.add(msg.escalation_id)
            self.storage.set(_WRITTEN_KEY, list(written))
        except Exception:
            logger.error("kb_accept storage_error escalation_id=%s", msg.escalation_id, exc_info=True)
            return AcceptResult(False, "storage_error")
        return AcceptResult(True, "accepted")

    def attribution_note(self, entry: KBEntry) -> str:
        return f"Expert answer ({entry.approver_display_name}, {entry.topic.value}, {entry.date})"

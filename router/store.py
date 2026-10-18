"""SQLite store of escalations (stdlib sqlite3, one table).

State machine: proposed -> approved -> sent -> answered, or proposed -> cancelled.
`transition` is a compare-and-set so "first click / first reply wins" (D8) holds
even if two Slack events arrive at once.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import asdict, dataclass, fields
from typing import Iterable, Optional

PROPOSED, APPROVED, SENT, ANSWERED, CANCELLED = "proposed", "approved", "sent", "answered", "cancelled"


@dataclass
class EscalationRecord:
    escalation_id: str
    asker_ref: str
    question: str
    topic: Optional[str]                 # Topic value; None when flagged
    flagged: bool
    has_draft: bool
    draft_answer: Optional[str]
    state: str
    proposed_expert_id: Optional[str]    # experts.Expert.id, or None for a channel/fixed fallback
    recipient_user_id: Optional[str]     # Slack ID mentioned in the expert post (None = channel only)
    recipient_name: Optional[str]
    recipient_role: Optional[str]
    channel_id: str                      # where the expert post goes / went
    thread_ts: Optional[str]             # ts of the expert post, once sent
    created_at: float
    sent_at: Optional[float] = None
    reminded: bool = False
    proposal_ts: Optional[str] = None    # ts of the proposal message in the asker's thread
    guard_reason: Optional[str] = None   # shown to the expert next to a draft


_COLUMNS = [f.name for f in fields(EscalationRecord)]
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS escalations (
    escalation_id TEXT PRIMARY KEY,
    {", ".join(c + " " for c in _COLUMNS[1:])}
)
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute(_SCHEMA)

    def add(self, rec: EscalationRecord) -> bool:
        """Insert; False if the escalation_id already exists (duplicate delivery)."""
        row = asdict(rec)
        with self._lock, self._db:
            cur = self._db.execute(
                f"INSERT OR IGNORE INTO escalations ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
                [row[c] for c in _COLUMNS],
            )
            return cur.rowcount == 1

    def get(self, escalation_id: str) -> Optional[EscalationRecord]:
        return self._one("SELECT * FROM escalations WHERE escalation_id = ?", (escalation_id,))

    def find_by_thread(self, channel_id: str, thread_ts: str) -> Optional[EscalationRecord]:
        return self._one("SELECT * FROM escalations WHERE channel_id = ? AND thread_ts = ?", (channel_id, thread_ts))

    def due_reminders(self, cutoff: float) -> list[EscalationRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM escalations WHERE state = ? AND reminded = 0 AND sent_at <= ?", (SENT, cutoff)
            ).fetchall()
        return [_to_record(r) for r in rows]

    def mark_reminded(self, escalation_id: str) -> bool:
        """Set `reminded` once; True only for the call that set it (one reminder, D8)."""
        with self._lock, self._db:
            return self._db.execute(
                "UPDATE escalations SET reminded = 1 WHERE escalation_id = ? AND state = ? AND reminded = 0",
                (escalation_id, SENT),
            ).rowcount == 1

    def update(self, escalation_id: str, **values) -> None:
        self._set(escalation_id, None, values)

    def transition(self, escalation_id: str, from_states: Iterable[str], to_state: str, **values) -> bool:
        """Move to `to_state` only if the current state is in `from_states`; True if this call won."""
        return self._set(escalation_id, tuple(from_states), {"state": to_state, **values})

    def _set(self, escalation_id: str, from_states: Optional[tuple], values: dict) -> bool:
        unknown = set(values) - set(_COLUMNS)
        if unknown:
            raise ValueError(f"unknown columns: {unknown}")
        sql = f"UPDATE escalations SET {', '.join(k + ' = ?' for k in values)} WHERE escalation_id = ?"
        args = [*values.values(), escalation_id]
        if from_states is not None:
            sql += f" AND state IN ({', '.join('?' for _ in from_states)})"
            args += list(from_states)
        with self._lock, self._db:
            return self._db.execute(sql, args).rowcount == 1

    def _one(self, sql: str, args: tuple) -> Optional[EscalationRecord]:
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        return _to_record(row) if row else None


def _to_record(row: sqlite3.Row) -> EscalationRecord:
    data = dict(row)
    for b in ("flagged", "has_draft", "reminded"):
        data[b] = bool(data[b])
    return EscalationRecord(**data)

"""Internal (non-wire) result types for the Vendor Agent's own flow."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from contracts import Escalation

Outcome = Literal["answered", "refused", "retry", "holding", "escalated"]


@dataclass
class VendorOutput:
    reply_text: Optional[str]
    outcome: Outcome
    escalation: Optional[Escalation]
    asker_ref: Optional[str]


@dataclass
class AcceptResult:
    accepted: bool
    reason: str

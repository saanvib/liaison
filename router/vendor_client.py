"""The Router's link to the Vendor Agent over the Chat Protocol (A2, A3, A8, D14, D31).

Question out:  "LIAISON-REF: <asker_ref>\n\n<question>"
Reply in:      "LIAISON-REF: <asker_ref>\nOUTCOME: <outcome>\n\n<body>"
On OUTCOME escalated the Vendor Agent also sends a typed Escalation with the same
asker_ref; both must arrive within VENDOR_WAIT_S (one budget). Anything not from
VENDOR_ADDRESS is logged and dropped.

The transport is injected (send_text, send_model) so tests run without uAgents.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from contracts import ApprovedAnswer, Escalation

log = logging.getLogger(__name__)

REF_PREFIX = "LIAISON-REF: "
OUTCOME_PREFIX = "OUTCOME: "
OUTCOMES = frozenset({"answered", "refused", "retry", "holding", "escalated"})

# What VendorClient.claim_escalation did with an Escalation.
DROPPED, CLAIMED, UNCLAIMED = "dropped", "claimed", "unclaimed"

SendText = Callable[[str, str], Awaitable[None]]        # (address, text)
SendModel = Callable[[str, object], Awaitable[None]]    # (address, uagents Model)


@dataclass(frozen=True)
class VendorReply:
    asker_ref: str
    outcome: str
    body: str


@dataclass(frozen=True)
class VendorResult:
    outcome: str
    body: str
    escalation: Optional[Escalation] = None


def build_question(asker_ref: str, question: str) -> str:
    return f"{REF_PREFIX}{asker_ref}\n\n{question}"


def parse_reply(text: str) -> VendorReply:
    """Parse the Vendor Agent's reply envelope; raises ValueError if it is malformed."""
    head, sep, body = text.partition("\n\n")
    lines = head.split("\n")
    if not sep and len(lines) == 2:
        body = ""  # an empty body with no trailing blank line
    if len(lines) != 2 or not lines[0].startswith(REF_PREFIX) or not lines[1].startswith(OUTCOME_PREFIX):
        raise ValueError(f"not a LIAISON reply envelope: {text[:80]!r}")
    asker_ref = lines[0][len(REF_PREFIX):].strip()
    outcome = lines[1][len(OUTCOME_PREFIX):].strip()
    if not asker_ref or outcome not in OUTCOMES:
        raise ValueError(f"bad envelope header: {head!r}")
    return VendorReply(asker_ref, outcome, body)


@dataclass
class _Pending:
    reply: asyncio.Future
    escalation: asyncio.Future
    early_escalation: Optional[Escalation] = field(default=None)


class VendorClient:
    def __init__(self, vendor_address: str, send_text: SendText, send_model: SendModel, wait_s: float = 30.0):
        self.vendor_address = vendor_address
        self._send_text = send_text
        self._send_model = send_model
        self.wait_s = wait_s
        self._pending: dict[str, _Pending] = {}

    async def ask(self, asker_ref: str, question: str) -> Optional[VendorResult]:
        """Ask and wait. None means no complete answer in time (the caller tells the asker)."""
        loop = asyncio.get_running_loop()
        pending = _Pending(loop.create_future(), loop.create_future())
        self._pending[asker_ref] = pending
        deadline = time.monotonic() + self.wait_s
        try:
            await self._send_text(self.vendor_address, build_question(asker_ref, question))
            reply: VendorReply = await asyncio.wait_for(pending.reply, self.wait_s)
            if reply.outcome != "escalated":
                return VendorResult(reply.outcome, reply.body)
            esc = pending.early_escalation or await asyncio.wait_for(
                pending.escalation, max(0.0, deadline - time.monotonic())
            )
            return VendorResult(reply.outcome, reply.body, esc)
        except asyncio.TimeoutError:
            log.error("vendor timeout after %.1fs for %s", self.wait_s, asker_ref)
            return None
        except Exception:
            log.exception("vendor ask failed for %s", asker_ref)
            return None
        finally:
            self._pending.pop(asker_ref, None)

    def on_chat_reply(self, sender: str, text: str) -> bool:
        """Feed a ChatMessage text from the agent; True if it completed a pending ask."""
        if sender != self.vendor_address:
            log.warning("dropped chat reply from untrusted sender %s", sender)
            return False
        try:
            reply = parse_reply(text)
        except ValueError as e:
            log.warning("dropped malformed vendor reply: %s", e)
            return False
        pending = self._pending.get(reply.asker_ref)
        if pending is None or pending.reply.done():
            log.warning("dropped vendor reply with no pending ask: %s", reply.asker_ref)
            return False
        pending.reply.set_result(reply)
        return True

    def claim_escalation(self, sender: str, esc: Escalation) -> str:
        """DROPPED (untrusted sender), CLAIMED (completes a pending ask) or UNCLAIMED (caller handles it)."""
        if sender != self.vendor_address:
            log.warning("dropped Escalation %s from untrusted sender %s", esc.escalation_id, sender)
            return DROPPED
        pending = self._pending.get(esc.asker_ref or "")
        if pending is None:
            return UNCLAIMED
        if pending.reply.done():
            if not pending.escalation.done():
                pending.escalation.set_result(esc)
        else:
            pending.early_escalation = esc  # arrived before the chat reply
        return CLAIMED

    async def send_approved_answer(self, msg: ApprovedAnswer) -> None:
        await self._send_model(self.vendor_address, msg)

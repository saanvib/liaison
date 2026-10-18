"""Router logic: question in, answer out; escalation proposal, asker approval, expert reply.

RouterCore never imports experts/, slack_bolt or uagents: the matcher, allowlist,
person lookup, Vendor link, Slack client, store and clock are all injected
(router/agent.py wires the real ones), so tests run offline with fakes.

Rules (docs/work-split/person-3-router-slack.md): nothing goes to an expert before the
asker approves (R16); a flagged question never calls the matcher and is never stored
(D23, A5); only the matched expert plus the topic allowlist may reply or click (D11);
the first allowed reply wins and one reminder goes out (D8); the answer goes back to
the Vendor Agent as ApprovedAnswer(guard_verdict=None) (A4, D18).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional, Protocol

from contracts import ApprovedAnswer, Escalation, EscalationReason, Topic

from router import texts
from router.config import Config
from router.store import (
    ANSWERED, APPROVED, CANCELLED, PROPOSED, SENT, EscalationRecord, Store,
)
from router.vendor_client import UNCLAIMED, VendorResult

log = logging.getLogger(__name__)

# Fallback role for an allowlisted replier we can't find in the expert directory.
UNKNOWN_ROLE = "Team member"


class SlackError(Exception):
    """Raised by a SlackPort when Slack rejects a call; `code` is Slack's error string."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SlackPort(Protocol):
    async def post(self, channel: str, text: str, thread_ts: Optional[str] = None,
                   blocks: Optional[list] = None) -> str: ...                          # returns the message ts
    async def update(self, channel: str, ts: str, text: str, blocks: Optional[list] = None) -> None: ...
    async def ephemeral(self, channel: str, user: str, text: str, thread_ts: Optional[str] = None) -> None: ...
    async def respond(self, response_url: str, text: str) -> None: ...                 # slash-command reply
    async def open_modal(self, trigger_id: str, view: dict) -> None: ...
    async def user_name(self, user_id: str) -> str: ...


class Vendor(Protocol):
    async def ask(self, asker_ref: str, question: str) -> Optional[VendorResult]: ...
    def claim_escalation(self, sender: str, esc: Escalation) -> str: ...
    async def send_approved_answer(self, msg: ApprovedAnswer) -> None: ...


Matcher = Callable[[str, Optional[Topic]], "MatchResultLike"]
Allowlist = Callable[[Topic], set]
# Slack user ID -> (display_name, role) from the expert directory, or None if unknown.
PersonLookup = Callable[[str], Optional[tuple[str, str]]]


class MatchResultLike(Protocol):  # experts.MatchResult
    expert: object
    topic: Topic
    score: float
    fallback: bool


def make_asker_ref(channel_id: str, thread_ts: str, user_id: str) -> str:
    return f"slack:{channel_id}:{thread_ts}:{user_id}"


def parse_asker_ref(asker_ref: Optional[str]) -> Optional[tuple[str, str, str]]:
    """(channel_id, thread_ts, user_id) for a Slack asker_ref (A3), else None."""
    parts = (asker_ref or "").split(":")
    if len(parts) != 4 or parts[0] != "slack" or not all(parts[1:]):
        return None
    return parts[1], parts[2], parts[3]


def is_flagged(esc: Escalation) -> bool:
    """A5: no verdict, no draft, reason GUARD_NEEDS_REVIEW."""
    return esc.guard_verdict is None and esc.draft_answer is None and esc.reason == EscalationReason.GUARD_NEEDS_REVIEW


class RouterCore:
    def __init__(self, matcher: Matcher, vendor: Vendor, slack: SlackPort, store: Store, config: Config,
                 clock: Callable[[], float], *, allowlist: Allowlist = lambda topic: set(),
                 lookup_person: PersonLookup = lambda user_id: None):
        self.matcher = matcher
        self.vendor = vendor
        self.slack = slack
        self.store = store
        self.config = config
        self.clock = clock
        self.allowlist = allowlist
        self.lookup_person = lookup_person

    # ---- questions -------------------------------------------------------------------

    async def on_ask_command(self, user_id: str, channel_id: str, text: str, response_url: str = "") -> None:
        """/ask <question>: post the question in the channel, then answer in that post's thread."""
        question = text.strip()
        if not question:
            await self.slack.respond(response_url, texts.ASK_USAGE)
            return
        try:
            ts = await self.slack.post(channel_id, texts.asked_via_command(user_id, question))
        except SlackError as e:
            log.warning("/ask could not post in %s: %s", channel_id, e.code)
            await self.slack.respond(response_url, texts.NOT_IN_CHANNEL)
            return
        await self.on_slack_question(user_id, channel_id, ts, question)

    async def on_mention(self, user_id: str, channel_id: str, thread_ts: str, text: str) -> None:
        """@Liaison <question>. A mention inside an escalation thread is a reply, handled by on_thread_reply."""
        if self.store.find_by_thread(channel_id, thread_ts):
            return
        await self.on_slack_question(user_id, channel_id, thread_ts, text)

    async def on_slack_question(self, user_id: str, channel_id: str, thread_ts: str, text: str) -> None:
        asker_ref = make_asker_ref(channel_id, thread_ts, user_id)
        result = await self.vendor.ask(asker_ref, text)
        if result is None:
            await self.slack.post(channel_id, texts.VENDOR_UNREACHABLE, thread_ts=thread_ts)
            return
        if result.outcome == "escalated":
            if result.escalation is None:
                await self.slack.post(channel_id, texts.VENDOR_UNREACHABLE, thread_ts=thread_ts)
                return
            await self.on_escalation(result.escalation)
            return
        await self.slack.post(channel_id, result.body or texts.VENDOR_UNREACHABLE, thread_ts=thread_ts)

    # ---- escalation and proposal (R16, D10, D23) ------------------------------------

    async def on_vendor_escalation(self, sender: str, esc: Escalation) -> None:
        """An Escalation from the agent: a pending ask takes it, otherwise handle it directly."""
        if self.vendor.claim_escalation(sender, esc) == UNCLAIMED:
            await self.on_escalation(esc)

    async def on_escalation(self, esc: Escalation) -> None:
        where = parse_asker_ref(esc.asker_ref)
        if where is None:
            log.warning("dropped Escalation %s: not a Slack asker_ref (%r)", esc.escalation_id, esc.asker_ref)
            return
        if self.store.get(esc.escalation_id):
            log.info("duplicate Escalation %s ignored", esc.escalation_id)
            return
        asker_channel, asker_thread, _ = where
        flagged = is_flagged(esc)

        if flagged:
            # D23: no matcher, no LLM; fixed reviewer in the fixed channel.
            topic, expert_id, name, role = None, None, None, None
            recipient_user = self.config.flagged_fallback_user
            channel_id = self.config.flagged_fallback_channel
            person = self.lookup_person(recipient_user)
            if person:
                name, role = person
        else:
            result = await asyncio.to_thread(self.matcher, esc.question, esc.topic)
            topic = result.topic
            channel_id = self.config.topic_channel(topic)
            expert = None if result.fallback else result.expert
            if expert is not None:
                expert_id, recipient_user = expert.id, expert.slack_user_id
                name, role = expert.display_name, expert.role
            else:
                expert_id = recipient_user = name = role = None

        rec = EscalationRecord(
            escalation_id=esc.escalation_id, asker_ref=esc.asker_ref, question=esc.question,
            topic=topic.value if topic else None, flagged=flagged,
            has_draft=esc.draft_answer is not None, draft_answer=esc.draft_answer, state=PROPOSED,
            proposed_expert_id=expert_id, recipient_user_id=recipient_user, recipient_name=name,
            recipient_role=role, channel_id=channel_id, thread_ts=None, created_at=self.clock(),
            guard_reason=self._guard_reason(esc),
        )
        if not self.store.add(rec):
            return
        recipient = texts.recipient_label(recipient_user, name, role, channel_id)
        text, blocks = texts.proposal_blocks(esc.escalation_id, esc.question, recipient, flagged)
        ts = await self.slack.post(asker_channel, text, thread_ts=asker_thread, blocks=blocks)
        self.store.update(esc.escalation_id, proposal_ts=ts)

    @staticmethod
    def _guard_reason(esc: Escalation) -> Optional[str]:
        if esc.draft_answer is None:
            return None
        if esc.guard_verdict is None:
            return "the compliance check gave no verdict in time"
        return f"compliance check {esc.guard_verdict.verdict.value}: {esc.guard_verdict.reason}"

    # ---- asker clicks ---------------------------------------------------------------

    async def on_approve_ask(self, escalation_id: str, user_id: str, channel_id: str) -> None:
        rec = await self._asker_only(escalation_id, user_id, channel_id)
        if rec is None or not self.store.transition(escalation_id, [PROPOSED], APPROVED):
            return
        asker_channel, asker_thread, _ = parse_asker_ref(rec.asker_ref)
        recipient = texts.recipient_label(rec.recipient_user_id, rec.recipient_name, rec.recipient_role, rec.channel_id)
        text, blocks = texts.expert_post(escalation_id, rec.question, rec.recipient_user_id, rec.flagged,
                                         rec.draft_answer, rec.guard_reason)
        try:
            ts = await self.slack.post(rec.channel_id, text, blocks=blocks)
        except SlackError as e:
            log.error("could not post escalation %s in %s: %s", escalation_id, rec.channel_id, e.code)
            self.store.transition(escalation_id, [APPROVED], PROPOSED)
            await self.slack.post(asker_channel, texts.EXPERT_CHANNEL_FAILED, thread_ts=asker_thread)
            return
        self.store.transition(escalation_id, [APPROVED], SENT, thread_ts=ts, sent_at=self.clock())
        if rec.proposal_ts:
            done = f"Sent to {recipient}. I'll post the answer here."
            await self.slack.update(asker_channel, rec.proposal_ts, done, blocks=texts.proposal_resolved(done))

    async def on_cancel_ask(self, escalation_id: str, user_id: str, channel_id: str) -> None:
        rec = await self._asker_only(escalation_id, user_id, channel_id)
        if rec is None or not self.store.transition(escalation_id, [PROPOSED], CANCELLED):
            return
        asker_channel, asker_thread, _ = parse_asker_ref(rec.asker_ref)
        if rec.proposal_ts:
            await self.slack.update(asker_channel, rec.proposal_ts, texts.CANCELLED,
                                    blocks=texts.proposal_resolved(texts.CANCELLED))
        else:
            await self.slack.post(asker_channel, texts.CANCELLED, thread_ts=asker_thread)

    async def _asker_only(self, escalation_id: str, user_id: str, channel_id: str) -> Optional[EscalationRecord]:
        rec = self.store.get(escalation_id)
        if rec is None:
            return None
        asker_channel, asker_thread, asker = parse_asker_ref(rec.asker_ref)
        if user_id != asker:
            await self.slack.ephemeral(channel_id, user_id, texts.ONLY_ASKER, thread_ts=asker_thread)
            return None
        return rec

    # ---- expert side (D8, D11, D17) ---------------------------------------------------

    def allowed_repliers(self, rec: EscalationRecord) -> set[str]:
        if rec.flagged:
            return {self.config.flagged_fallback_user}
        allowed = set(self.allowlist(Topic(rec.topic)))
        if rec.recipient_user_id:
            allowed.add(rec.recipient_user_id)
        return allowed

    async def on_thread_reply(self, channel_id: str, thread_ts: str, user_id: str, text: str) -> None:
        """A non-bot, non-edit message in a thread. Ignored unless it's an open escalation thread."""
        rec = self.store.find_by_thread(channel_id, thread_ts)
        if rec is None or rec.state != SENT or not text.strip():
            return
        if user_id not in self.allowed_repliers(rec):
            await self.slack.ephemeral(channel_id, user_id, texts.NOT_ALLOWED, thread_ts=thread_ts)
            return
        await self._finish(rec, user_id, text)

    async def on_approve_draft(self, escalation_id: str, user_id: str, channel_id: str) -> None:
        rec = await self._expert_only(escalation_id, user_id, channel_id)
        if rec and rec.draft_answer:
            await self._finish(rec, user_id, rec.draft_answer)

    async def on_edit_draft(self, escalation_id: str, user_id: str, channel_id: str, trigger_id: str) -> None:
        rec = await self._expert_only(escalation_id, user_id, channel_id)
        if rec:
            await self.slack.open_modal(trigger_id, texts.edit_modal(escalation_id, rec.draft_answer or ""))

    async def on_edit_submit(self, escalation_id: str, user_id: str, text: str) -> None:
        """Edit modal submitted: the typed text is the final answer, with no re-check (D17)."""
        rec = self.store.get(escalation_id)
        if rec is None or rec.state != SENT or user_id not in self.allowed_repliers(rec) or not text.strip():
            return
        await self._finish(rec, user_id, text)

    async def _expert_only(self, escalation_id: str, user_id: str, channel_id: str) -> Optional[EscalationRecord]:
        rec = self.store.get(escalation_id)
        if rec is None:
            return None
        if user_id not in self.allowed_repliers(rec):
            await self.slack.ephemeral(channel_id, user_id, texts.NOT_ALLOWED, thread_ts=rec.thread_ts)
            return None
        if rec.state != SENT:
            await self.slack.ephemeral(channel_id, user_id, texts.ALREADY_DONE, thread_ts=rec.thread_ts)
            return None
        return rec

    async def _finish(self, rec: EscalationRecord, user_id: str, answer: str) -> None:
        if not self.store.transition(rec.escalation_id, [SENT], ANSWERED):
            return  # someone else's reply won (D8)
        name, role = await self._who(rec, user_id)
        asker_channel, asker_thread, _ = parse_asker_ref(rec.asker_ref)
        await self.slack.post(asker_channel, texts.answer_for_asker(name, role, answer), thread_ts=asker_thread)
        await self.slack.post(rec.channel_id, texts.expert_thanks(rec.flagged), thread_ts=rec.thread_ts)
        if rec.flagged:
            log.info("flagged escalation %s answered; not sent to the KB (D23)", rec.escalation_id)
            return
        try:
            msg = ApprovedAnswer(
                escalation_id=rec.escalation_id, question=rec.question, answer=answer, topic=Topic(rec.topic),
                approved_by=f"{user_id}|{name}", guard_verdict=None, citations=[], asker_ref=rec.asker_ref,
            )
            await self.vendor.send_approved_answer(msg)
        except Exception:
            log.exception("could not send ApprovedAnswer %s to the Vendor Agent", rec.escalation_id)

    async def _who(self, rec: EscalationRecord, user_id: str) -> tuple[str, Optional[str]]:
        if user_id == rec.recipient_user_id and rec.recipient_name:
            return rec.recipient_name, rec.recipient_role
        person = self.lookup_person(user_id)
        if person:
            return person
        try:
            return await self.slack.user_name(user_id), UNKNOWN_ROLE
        except Exception:
            return user_id, UNKNOWN_ROLE

    # ---- reminder (D8) ----------------------------------------------------------------

    async def tick(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        for rec in self.store.due_reminders(now - self.config.reminder_after_s):
            if self.store.mark_reminded(rec.escalation_id):
                await self.slack.post(rec.channel_id, texts.reminder(rec.recipient_user_id), thread_ts=rec.thread_ts)

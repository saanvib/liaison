"""agent.py: uAgents wiring for the Vendor Agent. No business logic here --
VendorCore.start / advance / handle_approved_answer / handle_ingest do all of
that; this file only moves messages in and out (D4, D7; A2, A8, A10).

    python -m vendor_agent.agent          # needs the env in vendor_agent/.env.example

Each Guard call is its own round of messages: on_chat sends the screen
GuardRequest and returns, on_verdict picks the question back up from storage,
and expire_guard_checks treats a check with no verdict by its deadline as a
timeout. Nothing waits in memory: hosted Agentverse keeps no in-memory state
between messages (an awaited Future or a background task never saw its verdict,
2026-10-18), and uAgents runs one handler at a time, so awaiting a verdict inside
a handler would block the very message that carries it.

Verified on uagents 0.26.1: ChatMessage has timestamp, msg_id, content;
ChatAcknowledgement has timestamp, acknowledged_msg_id, metadata.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from uagents import Agent, Context, Protocol
from uagents.crypto import Identity
from uagents.storage import KeyValueStore
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)

from contracts import ApprovedAnswer, GuardVerdict, IngestDocument

from vendor_agent.clients import LLMClient, default_embedder
from vendor_agent.config import Config
from vendor_agent.core import PendingCheck, VendorCore
from vendor_agent.kb import KnowledgeBase
from vendor_agent.models import VendorOutput
from vendor_agent.retrieval import Retriever

log = logging.getLogger(__name__)

REF_PREFIX = "LIAISON-REF: "
OUTCOME_PREFIX = "OUTCOME: "

SWEEP_PERIOD_S = 2.0  # how often expire_guard_checks looks for checks past their deadline
_PENDING_PREFIX = "vendor_agent:guard_pending:"
_PENDING_IDS_KEY = "vendor_agent:guard_pending_ids"


def build_reply_envelope(asker_ref: str, outcome: str, body: str) -> str:
    """A2 reply envelope, parsed by router/vendor_client.py's parse_reply."""
    return f"{REF_PREFIX}{asker_ref}\n{OUTCOME_PREFIX}{outcome}\n\n{body}"


def parse_question_envelope(text: str) -> tuple[str, str]:
    """A2 question envelope, as built by router/vendor_client.py's build_question.
    Raises ValueError if malformed."""
    head, sep, body = text.partition("\n\n")
    if not sep or not head.startswith(REF_PREFIX):
        raise ValueError(f"not a LIAISON question envelope: {text[:80]!r}")
    asker_ref = head[len(REF_PREFIX):].strip()
    if not asker_ref:
        raise ValueError(f"empty asker_ref in envelope: {head!r}")
    return asker_ref, body


class PendingChecks:
    """PendingChecks waiting on a Guard verdict, in agent storage, keyed by request_id,
    each with a deadline. Storage has no key listing, so the ids are kept in a list too."""

    def __init__(self, storage, now: Callable[[], float] = time.time):
        self.storage = storage
        self.now = now

    def add(self, check: PendingCheck, timeout_s: float) -> None:
        request_id = check.request.request_id
        self.storage.set(_PENDING_PREFIX + request_id, {"check": check.to_dict(), "deadline": self.now() + timeout_s})
        self.storage.set(_PENDING_IDS_KEY, (self.storage.get(_PENDING_IDS_KEY) or []) + [request_id])

    def pop(self, request_id: str) -> Optional[PendingCheck]:
        record = self.storage.get(_PENDING_PREFIX + request_id)
        self.storage.set(_PENDING_PREFIX + request_id, None)
        self.storage.set(_PENDING_IDS_KEY, [i for i in self.storage.get(_PENDING_IDS_KEY) or [] if i != request_id])
        return PendingCheck.from_dict(record["check"]) if record else None

    def pop_expired(self) -> list[PendingCheck]:
        now = self.now()
        expired = []
        for request_id in self.storage.get(_PENDING_IDS_KEY) or []:
            record = self.storage.get(_PENDING_PREFIX + request_id)
            if record is None or record["deadline"] <= now:
                check = self.pop(request_id)
                if check is not None:
                    expired.append(check)
        return expired


def _vendor_storage(seed: str) -> KeyValueStore:
    """The same on-disk store a live Agent(seed=seed) would use for ctx.storage
    (both are keyed by the seed-derived address), so python -m vendor_agent.ingest
    --direct and a later python -m vendor_agent.agent run share one KB."""
    address = Identity.from_seed(seed, 0).address
    return KeyValueStore(name=address)


def build_core(storage=None, guard=None, router=None, config: Optional[Config] = None) -> VendorCore:
    """Build a real VendorCore from env. guard/router default to None, which is
    fine for the agent wiring (it sends Guard and Router messages itself) and for
    ingest.py --direct (it only uses handle_ingest)."""
    config = config or Config.from_env()
    storage = storage if storage is not None else _vendor_storage(config.vendor_seed)
    embedder = default_embedder(config)
    llm = LLMClient(api_key=config.llm_api_key, model=config.llm_model)
    kb = KnowledgeBase(storage, embedder, config)
    retriever = Retriever(embedder, storage)
    return VendorCore(
        kb, retriever, llm, guard, router, storage, config, clock=lambda: datetime.now(timezone.utc)
    )


def wire(
    agent: Agent, config: Config, core: Optional[VendorCore] = None, storage=None,
    now: Callable[[], float] = time.time,
) -> VendorCore:
    """Attach every handler to `agent` (local or hosted-predefined) and return the
    VendorCore it ends up using. Split out from create_agent() so the hosted
    entry point (vendor_agent/hosted/entry.py) can call this on Agentverse's
    predefined `agent` without constructing its own.

    `storage` lets the hosted entry point pass `agent.storage` directly (no
    VENDOR_SEED hosted -- Agentverse assigns the address, so there's nothing to
    derive a KeyValueStore name from); local use derives one from VENDOR_SEED.
    Pending Guard checks live in the core's storage, next to the KB."""
    if core is None:
        core = build_core(
            storage=storage if storage is not None else _vendor_storage(config.vendor_seed), config=config,
        )
    pending = PendingChecks(core.storage, now=now)

    async def ask_guard(ctx: Context, check: PendingCheck) -> None:
        # Saved before sending: the verdict may be handled before this handler returns.
        pending.add(check, config.guard_timeout_s)
        await ctx.send(config.guard_address, check.request)

    async def deliver(ctx: Context, check: PendingCheck, output: VendorOutput) -> None:
        if check.sender == config.router_address:
            reply_text = build_reply_envelope(check.asker_ref, output.outcome, output.reply_text or "")
        else:
            reply_text = output.reply_text or ""
        await ctx.send(check.sender, ChatMessage(content=[TextContent(text=reply_text)]))
        if output.escalation is not None:
            await ctx.send(config.router_address, output.escalation)

    async def resume(ctx: Context, check: PendingCheck, verdict: Optional[GuardVerdict]) -> None:
        step = core.advance(check, verdict)
        if isinstance(step, PendingCheck):
            await ask_guard(ctx, step)
        else:
            await deliver(ctx, check, step)

    chat_proto = Protocol(spec=chat_protocol_spec)

    @chat_proto.on_message(ChatMessage)
    async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
        await ctx.send(sender, ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id
        ))
        text = msg.text()
        if sender == config.router_address:
            try:
                asker_ref, question = parse_question_envelope(text)
            except ValueError:
                ctx.logger.warning(f"dropped malformed question envelope from {sender}")
                return
        else:
            asker_ref, question = f"chat:{sender}", text
        await ask_guard(ctx, core.start(question, sender, asker_ref))

    @chat_proto.on_message(ChatAcknowledgement)
    async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
        ctx.logger.debug(f"ack from {sender} for {msg.acknowledged_msg_id}")

    agent.include(chat_proto, publish_manifest=True)

    @agent.on_message(model=ApprovedAnswer)
    async def on_approved_answer(ctx: Context, sender: str, msg: ApprovedAnswer):
        result = await core.handle_approved_answer(msg, sender)
        ctx.logger.info(f"handle_approved_answer accepted={result.accepted} reason={result.reason}")

    @agent.on_message(model=IngestDocument)
    async def on_ingest(ctx: Context, sender: str, msg: IngestDocument):
        await core.handle_ingest(msg, sender)

    @agent.on_message(model=GuardVerdict)
    async def on_verdict(ctx: Context, sender: str, msg: GuardVerdict):
        if sender != config.guard_address:
            ctx.logger.warning(f"dropped GuardVerdict from {sender}, not the Guard")
            return
        check = pending.pop(msg.request_id)
        if check is None:
            ctx.logger.info(f"late GuardVerdict for {msg.request_id}: its check already timed out")
            return
        await resume(ctx, check, msg)

    @agent.on_interval(period=SWEEP_PERIOD_S)
    async def expire_guard_checks(ctx: Context):
        for check in pending.pop_expired():
            ctx.logger.info(f"no GuardVerdict for {check.request.request_id} ({check.stage}) in time")
            await resume(ctx, check, None)

    return core


def create_agent(
    config: Optional[Config] = None, core: Optional[VendorCore] = None, **agent_kwargs
) -> tuple[Agent, VendorCore]:
    config = config or Config.from_env()
    agent = Agent(
        name="liaison-vendor",
        seed=config.vendor_seed,
        description=(
            "Liaison Vendor Agent: answers product questions with citations, "
            "serves KB answers, escalates to a human when it can't answer, and "
            "checks every question and answer with a Compliance Guard."
        ),
        **agent_kwargs,
    )
    core = wire(agent, config, core)
    return agent, core


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    vendor, _ = create_agent()
    print(f"Vendor address: {vendor.address}")
    vendor.run()

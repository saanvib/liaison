"""agent.py: uAgents wiring for the Vendor Agent. No business logic here --
VendorCore.handle_question / handle_approved_answer / handle_ingest do all of
that; this file only moves messages in and out (D4, D7; A2, A8, A10).

    python -m vendor_agent.agent          # needs the env in vendor_agent/.env.example

Verified on uagents 0.26.1: ChatMessage has timestamp, msg_id, content;
ChatAcknowledgement has timestamp, acknowledged_msg_id, metadata.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from uagents import Agent, Context, Protocol
from uagents.crypto import Identity
from uagents.storage import KeyValueStore
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)

from contracts import ApprovedAnswer, Escalation, GuardRequest, GuardVerdict, IngestDocument

from vendor_agent.clients import LLMClient, default_embedder
from vendor_agent.config import Config
from vendor_agent.core import VendorCore
from vendor_agent.kb import KnowledgeBase
from vendor_agent.retrieval import Retriever

log = logging.getLogger(__name__)

REF_PREFIX = "LIAISON-REF: "
OUTCOME_PREFIX = "OUTCOME: "

SendModel = Callable[[str, object], Awaitable[None]]
SendText = Callable[[str, str], Awaitable[None]]


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


class UAgentGuardClient:
    """GuardClient: sends a GuardRequest to guard_address, awaits a Future keyed
    by request_id, returns None on timeout. Transport is injected (send_model),
    same pattern as router/vendor_client.py's VendorClient, so this needs no
    live uAgents Context to construct."""

    def __init__(self, guard_address: str, send_model: SendModel):
        self.guard_address = guard_address
        self._send_model = send_model
        self._pending: dict[str, asyncio.Future] = {}

    async def check(self, req: GuardRequest, timeout_s: float = 10.0) -> Optional[GuardVerdict]:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[req.request_id] = future
        try:
            await self._send_model(self.guard_address, req)
            return await asyncio.wait_for(future, timeout_s)
        except asyncio.TimeoutError:
            return None
        except Exception:
            log.exception("guard check failed for request_id=%s", req.request_id)
            return None
        finally:
            self._pending.pop(req.request_id, None)

    def on_verdict(self, verdict: GuardVerdict) -> None:
        future = self._pending.get(verdict.request_id)
        if future is not None and not future.done():
            future.set_result(verdict)


class UAgentRouterClient:
    """RouterClient: sends Escalations to router_address, and chat replies to
    whoever asked (the Router for internal askers, the asker's own address
    for external ones)."""

    def __init__(self, router_address: str, send_model: SendModel, send_text: SendText):
        self.router_address = router_address
        self._send_model = send_model
        self._send_text = send_text

    async def send_escalation(self, esc: Escalation) -> None:
        await self._send_model(self.router_address, esc)

    async def reply_chat(self, to_sender: str, text: str) -> None:
        await self._send_text(to_sender, text)


def _vendor_storage(seed: str) -> KeyValueStore:
    """The same on-disk store a live Agent(seed=seed) would use for ctx.storage
    (both are keyed by the seed-derived address), so python -m vendor_agent.ingest
    --direct and a later python -m vendor_agent.agent run share one KB."""
    address = Identity.from_seed(seed, 0).address
    return KeyValueStore(name=address)


def build_core(storage=None, guard=None, router=None, config: Optional[Config] = None) -> VendorCore:
    """Build a real VendorCore from env. guard/router default to None, which is
    fine for callers (ingest.py --direct) that only use handle_ingest."""
    config = config or Config.from_env()
    storage = storage if storage is not None else _vendor_storage(config.vendor_seed)
    embedder = default_embedder(config)
    llm = LLMClient(api_key=config.llm_api_key, model=config.llm_model)
    kb = KnowledgeBase(storage, embedder, config)
    retriever = Retriever(embedder, storage)
    return VendorCore(
        kb, retriever, llm, guard, router, storage, config, clock=lambda: datetime.now(timezone.utc)
    )


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

    runtime: dict = {"ctx": None}

    async def send_text(address: str, text: str) -> None:
        await runtime["ctx"].send(address, ChatMessage(content=[TextContent(text=text)]))

    async def send_model(address: str, model) -> None:
        await runtime["ctx"].send(address, model)

    guard_client = UAgentGuardClient(config.guard_address, send_model)
    router_client = UAgentRouterClient(config.router_address, send_model, send_text)

    if core is None:
        core = build_core(
            storage=_vendor_storage(config.vendor_seed), guard=guard_client, router=router_client, config=config
        )

    chat_proto = Protocol(spec=chat_protocol_spec)

    @chat_proto.on_message(ChatMessage)
    async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
        runtime["ctx"] = ctx  # a message handler always has a valid ctx; don't rely on startup winning a race
        await ctx.send(sender, ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id
        ))
        text = msg.text()
        internal = sender == config.router_address

        if internal:
            try:
                asker_ref, question = parse_question_envelope(text)
            except ValueError:
                ctx.logger.warning("dropped malformed question envelope from %s", sender)
                return
        else:
            asker_ref, question = f"chat:{sender}", text

        output = await core.handle_question(question, sender, asker_ref)

        if internal:
            reply_text = build_reply_envelope(asker_ref, output.outcome, output.reply_text or "")
        else:
            reply_text = output.reply_text or ""
        await router_client.reply_chat(sender, reply_text)
        if output.escalation is not None:
            await router_client.send_escalation(output.escalation)

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
        guard_client.on_verdict(msg)

    @agent.on_event("startup")
    async def on_startup(ctx: Context):
        runtime["ctx"] = ctx

    return agent, core


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    vendor, _ = create_agent()
    print(f"Vendor address: {vendor.address}")
    vendor.run()

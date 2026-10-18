"""on_chat must not block while the question waits for the Guard.

uAgents runs one message handler at a time by default, and the GuardVerdict arrives as a
separate message. If on_chat awaited the whole answer, on_verdict could only run after the
Guard wait had timed out, and every question became "retry" (seen live on 2026-10-18).
"""
import asyncio
import logging

from uagents import Agent, Model
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, TextContent

from vendor_agent.agent import build_reply_envelope, wire
from vendor_agent.config import Config
from vendor_agent.models import VendorOutput

ROUTER = "agent1router"
REF = "slack:C1:1792297350.247049:U1"


class FakeCtx:
    def __init__(self):
        self.sent = []
        self.logger = logging.getLogger("fake-ctx")

    async def send(self, destination, message):
        self.sent.append((destination, message))


class GuardGatedCore:
    """handle_question waits until the test releases it, the way it waits for a GuardVerdict."""

    def __init__(self, fail=False):
        self.guard_answered = asyncio.Event()
        self.fail = fail

    async def handle_question(self, text, sender, asker_ref):
        await self.guard_answered.wait()
        if self.fail:
            raise RuntimeError("boom")
        return VendorOutput(reply_text=f"answer to {text}", outcome="answered", escalation=None, asker_ref=asker_ref)


def chat_handler(agent):
    digest = Model.build_schema_digest(ChatMessage)
    for proto in agent.protocols.values():
        if digest in proto.signed_message_handlers:
            return proto.signed_message_handlers[digest]
    raise AssertionError("no ChatMessage handler")


def wired(core):
    config = Config(router_address=ROUTER, guard_address="g", ingest_address="i", approver_allowlist=frozenset())
    agent = Agent(name="vendor-wiring-test", seed="vendor-wiring-test-seed")
    wire(agent, config, core=core)
    return chat_handler(agent)


def question():
    return ChatMessage(content=[TextContent(text=f"LIAISON-REF: {REF}\n\nWhat is the shelf life?")])


async def test_on_chat_returns_before_the_guard_answers():
    core = GuardGatedCore()
    on_chat, ctx, msg = wired(core), FakeCtx(), question()
    await asyncio.wait_for(on_chat(ctx, ROUTER, msg), 1.0)  # old code blocked here until the Guard wait timed out
    assert [type(m) for _, m in ctx.sent] == [ChatAcknowledgement]

    core.guard_answered.set()  # what on_verdict does once it can run
    for _ in range(20):
        if len(ctx.sent) == 2:
            break
        await asyncio.sleep(0.01)
    dest, reply = ctx.sent[1]
    assert dest == ROUTER
    assert reply.text() == build_reply_envelope(REF, "answered", "answer to What is the shelf life?")


async def test_background_failure_is_logged(caplog):
    core = GuardGatedCore(fail=True)
    on_chat, ctx = wired(core), FakeCtx()
    await asyncio.wait_for(on_chat(ctx, ROUTER, question()), 1.0)
    core.guard_answered.set()
    for _ in range(20):
        if "answering a chat message failed" in caplog.text:
            break
        await asyncio.sleep(0.01)
    assert "answering a chat message failed" in caplog.text

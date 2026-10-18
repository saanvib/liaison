"""uAgents wiring: handlers reach the core / VendorClient. Handlers are called directly; no network."""
import logging

from uagents import Model
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, TextContent

from contracts import Escalation

from router.agent import create_agent, person_lookup
from router.tests.conftest import ASKER_REF, VENDOR, Expert, escalation, make_config


class FakeCtx:
    def __init__(self):
        self.sent = []
        self.logger = logging.getLogger("fake-ctx")

    async def send(self, destination, message):
        self.sent.append((destination, message))


class SpyCore:
    def __init__(self):
        self.escalations = []

    async def on_vendor_escalation(self, sender, msg):
        self.escalations.append((sender, msg))


def handler(agent, model):
    digest = Model.build_schema_digest(model)
    for proto in [agent._protocol, *agent.protocols.values()]:
        for table in (proto.signed_message_handlers, proto.unsigned_message_handlers):
            if digest in table:
                return table[digest]
    raise AssertionError(f"no handler for {model.__name__}")


def build():
    core = SpyCore()
    agent, _ = create_agent(make_config(), core=core, start_slack=False)
    return agent, core


async def test_hand_sent_escalation_reaches_core():
    agent, core = build()
    esc = escalation()
    await handler(agent, Escalation)(FakeCtx(), VENDOR, esc)
    assert core.escalations == [(VENDOR, esc)]


async def test_chat_message_is_acknowledged():
    agent, _ = build()
    ctx, msg = FakeCtx(), ChatMessage(content=[TextContent(text=f"LIAISON-REF: {ASKER_REF}\nOUTCOME: answered\n\nhi")])
    await handler(agent, ChatMessage)(ctx, VENDOR, msg)
    (dest, ack), = ctx.sent
    assert dest == VENDOR and isinstance(ack, ChatAcknowledgement) and ack.acknowledged_msg_id == msg.msg_id


async def test_agent_name_and_person_lookup():
    agent, _ = build()
    assert agent.name == "liaison-router" and agent.address.startswith("agent1")
    lookup = person_lookup([Expert("x", "U1", "Dr. X", "Lead", [])])
    assert lookup("U1") == ("Dr. X", "Lead") and lookup("U2") is None

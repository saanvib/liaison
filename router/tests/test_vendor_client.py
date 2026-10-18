"""VendorClient over a fake transport: envelope, outcomes, timeouts, sender trust (A2, A8, D14, T10)."""
import asyncio

import pytest

from router.vendor_client import (
    CLAIMED, DROPPED, UNCLAIMED, VendorClient, build_question, parse_reply,
)
from router.tests.conftest import ASKER_REF, VENDOR, escalation


def reply(outcome, body="the body", ref=ASKER_REF):
    return f"LIAISON-REF: {ref}\nOUTCOME: {outcome}\n\n{body}"


def test_build_question():
    assert build_question(ASKER_REF, "Q?") == f"LIAISON-REF: {ASKER_REF}\n\nQ?"


def test_parse_reply_keeps_body_and_colon_ref():
    r = parse_reply(reply("answered", "line 1\n\nline 2"))
    assert (r.asker_ref, r.outcome, r.body) == (ASKER_REF, "answered", "line 1\n\nline 2")


@pytest.mark.parametrize("bad", [
    "plain text", f"LIAISON-REF: {ASKER_REF}\n\nbody", f"LIAISON-REF: {ASKER_REF}\nOUTCOME: nope\n\nx",
    "LIAISON-REF: \nOUTCOME: answered\n\nx",
])
def test_parse_reply_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_reply(bad)


class Transport:
    """Fake transport: records sends; `respond` scripts what the 'Vendor' does after each question."""

    def __init__(self):
        self.sent, self.models, self.client, self.respond = [], [], None, None

    async def send_text(self, address, text):
        self.sent.append((address, text))
        if self.respond:
            asyncio.get_running_loop().call_soon(self.respond)

    async def send_model(self, address, model):
        self.models.append((address, model))


def make(wait_s=0.2):
    t = Transport()
    t.client = VendorClient(VENDOR, t.send_text, t.send_model, wait_s=wait_s)
    return t, t.client


@pytest.mark.parametrize("outcome", ["answered", "refused", "retry", "holding"])
async def test_simple_outcomes(outcome):
    t, c = make()
    t.respond = lambda: c.on_chat_reply(VENDOR, reply(outcome))
    result = await c.ask(ASKER_REF, "Q?")
    assert t.sent == [(VENDOR, build_question(ASKER_REF, "Q?"))]
    assert (result.outcome, result.body, result.escalation) == (outcome, "the body", None)


async def test_escalated_then_escalation():
    t, c = make()
    esc = escalation()

    def vendor():
        c.on_chat_reply(VENDOR, reply("escalated"))
        asyncio.get_running_loop().call_later(0.01, lambda: c.claim_escalation(VENDOR, esc))

    t.respond = vendor
    result = await c.ask(ASKER_REF, "Q?")
    assert result.outcome == "escalated" and result.escalation == esc


async def test_escalation_before_reply_is_kept():
    t, c = make()
    esc = escalation()

    def vendor():
        assert c.claim_escalation(VENDOR, esc) == CLAIMED
        c.on_chat_reply(VENDOR, reply("escalated"))

    t.respond = vendor
    assert (await c.ask(ASKER_REF, "Q?")).escalation == esc


async def test_escalated_without_escalation_times_out():
    t, c = make(wait_s=0.05)
    t.respond = lambda: c.on_chat_reply(VENDOR, reply("escalated"))
    assert await c.ask(ASKER_REF, "Q?") is None


async def test_total_timeout(caplog):
    t, c = make(wait_s=0.05)
    assert await c.ask(ASKER_REF, "Q?") is None
    assert "vendor timeout" in caplog.text


async def test_wrong_sender_dropped():
    t, c = make(wait_s=0.05)
    t.respond = lambda: c.on_chat_reply("agent1evil", reply("answered"))
    assert await c.ask(ASKER_REF, "Q?") is None
    assert c.claim_escalation("agent1evil", escalation()) == DROPPED


def test_unclaimed_escalation_and_stray_reply():
    t, c = make()
    assert c.claim_escalation(VENDOR, escalation()) == UNCLAIMED
    assert c.on_chat_reply(VENDOR, reply("answered")) is False


async def test_send_approved_answer_goes_to_vendor():
    t, c = make()
    await c.send_approved_answer("msg")
    assert t.models == [(VENDOR, "msg")]

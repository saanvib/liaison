import logging

import pytest

from contracts import GuardCategory, GuardRequest, Source, Verdict
from guard.agent import create_agent, handle_request, parse_allowed_senders
from guard.core import GuardCore

VENDOR, ROUTER, STRANGER = "agent1vendor", "agent1router", "agent1stranger"


class FakeCtx:
    def __init__(self):
        self.sent = []
        self.logger = logging.getLogger("fake-ctx")

    async def send(self, destination, message):
        self.sent.append((destination, message))


def screen(question="How do I store the kit?", rid="r1"):
    return GuardRequest(request_id=rid, source=Source.VENDOR_AGENT, question=question, draft_answer="")


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, frozenset()),
        ("", frozenset()),
        (" , ", frozenset()),
        ("agent1a", frozenset({"agent1a"})),
        ("agent1a, agent1b ,", frozenset({"agent1a", "agent1b"})),
    ],
)
def test_parse_allowed_senders(value, expected):
    assert parse_allowed_senders(value) == expected


async def test_allowed_sender_gets_verdict_back():
    ctx = FakeCtx()
    v = await handle_request(ctx, VENDOR, screen("Patient DOB 1/2/1970"), GuardCore(), frozenset({VENDOR, ROUTER}))
    assert v.verdict == Verdict.BLOCKED and v.category == GuardCategory.PATIENT_DATA
    assert ctx.sent == [(VENDOR, v)]
    assert v.request_id == "r1"


async def test_unlisted_sender_is_dropped_without_reply(caplog):
    ctx = FakeCtx()
    with caplog.at_level(logging.WARNING, logger="fake-ctx"):
        v = await handle_request(ctx, STRANGER, screen(), GuardCore(), frozenset({VENDOR, ROUTER}))
    assert v is None and ctx.sent == []
    assert "dropped" in caplog.text and STRANGER in caplog.text


async def test_empty_allowlist_drops_everything():
    ctx = FakeCtx()
    assert await handle_request(ctx, VENDOR, screen(), GuardCore(), frozenset()) is None
    assert ctx.sent == []


def test_create_agent_requires_seed(monkeypatch):
    monkeypatch.delenv("GUARD_SEED", raising=False)
    with pytest.raises(SystemExit):
        create_agent(allowed=frozenset())


async def test_create_agent_has_stable_address_from_seed():  # async: Agent() needs a running loop
    a = create_agent(seed="liaison-dev-only test seed", allowed=frozenset(), core=GuardCore(), port=8199)
    b = create_agent(seed="liaison-dev-only test seed", allowed=frozenset(), core=GuardCore(), port=8199)
    assert a.address == b.address and a.address.startswith("agent1")
    assert a.name == "liaison-guard"

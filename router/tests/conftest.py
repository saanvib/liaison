"""Fakes for the Router tests: Slack, Vendor link, matcher, clock. No network."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from contracts import ApprovedAnswer, Escalation, EscalationReason, GuardVerdict, Topic, Verdict

from router.config import Config
from router.core import RouterCore, SlackError
from router.store import Store
from router.vendor_client import DROPPED, UNCLAIMED, VendorResult

VENDOR = "agent1vendor"
ASKER, OTHER = "U_ASKER", "U_OTHER"
ASK_CHANNEL, ASK_TS = "C_ASK", "1700000000.000100"
ASKER_REF = f"slack:{ASK_CHANNEL}:{ASK_TS}:{ASKER}"

# ApprovedAnswer.guard_verdict becomes Optional in Person 1's PR0; until then a None verdict can't be built.
PR0_MERGED = not ApprovedAnswer.__fields__["guard_verdict"].required
needs_pr0 = pytest.mark.skipif(not PR0_MERGED, reason="waits for PR0: ApprovedAnswer.guard_verdict Optional (Person 1)")


def make_config(**overrides) -> Config:
    env = dict(
        SLACK_BOT_TOKEN="xoxb-test", SLACK_APP_TOKEN="xapp-test", VENDOR_ADDRESS=VENDOR, ROUTER_SEED="seed",
        CHANNEL_RD="C_RD", CHANNEL_QA_RA="C_QARA", CHANNEL_ENGINEERING="C_ENG", CHANNEL_SALES_OPS="C_SALES",
        FLAGGED_FALLBACK_CHANNEL="C_REVIEW", FLAGGED_FALLBACK_USER="U_REVIEWER", ROUTER_STORE_PATH=":memory:",
    )
    env.update(overrides)
    return Config.from_env(env)


@dataclass
class Expert:  # same shape as experts.Expert
    id: str
    slack_user_id: str
    display_name: str
    role: str
    topics: list
    summary: str = ""


@dataclass
class MatchResult:  # same shape as experts.MatchResult
    expert: Optional[Expert]
    topic: Topic
    score: float
    fallback: bool


RIVERA = Expert("rnd-rivera", "U_RND_1", "Dr. Rivera", "Senior Scientist, Assay Development", [Topic.RND])
ALLOWLIST = {Topic.RND: {"U_RND_1", "U_RND_2"}, Topic.SALES_OPS: {"U_SALES_1"}}
DIRECTORY = {"U_RND_2": ("Dr. Natarajan", "Application Scientist"), "U_REVIEWER": ("Pat Reviewer", "Compliance Lead")}


class FakeMatcher:
    def __init__(self, result: Optional[MatchResult] = None):
        self.result = result or MatchResult(RIVERA, Topic.RND, 0.8, False)
        self.calls: list = []

    def __call__(self, question, topic):
        self.calls.append((question, topic))
        return self.result


class FakeSlack:
    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self.ephemerals: list[dict] = []
        self.responses: list[str] = []
        self.modals: list[dict] = []
        self.fail_channels: set[str] = set()
        self._n = 0

    async def post(self, channel, text, thread_ts=None, blocks=None):
        if channel in self.fail_channels:
            raise SlackError("not_in_channel")
        self._n += 1
        ts = f"1800000000.{self._n:06d}"
        self.posts.append(dict(channel=channel, text=text, thread_ts=thread_ts, blocks=blocks, ts=ts))
        return ts

    async def update(self, channel, ts, text, blocks=None):
        self.updates.append(dict(channel=channel, ts=ts, text=text, blocks=blocks))

    async def ephemeral(self, channel, user, text, thread_ts=None):
        self.ephemerals.append(dict(channel=channel, user=user, text=text, thread_ts=thread_ts))

    async def respond(self, response_url, text):
        self.responses.append(text)

    async def open_modal(self, trigger_id, view):
        self.modals.append(dict(trigger_id=trigger_id, view=view))

    async def user_name(self, user_id):
        return f"name-of-{user_id}"

    def in_channel(self, channel):
        return [p for p in self.posts if p["channel"] == channel]

    def action_ids(self, post):
        return [e["action_id"] for b in post["blocks"] or [] if b["type"] == "actions" for e in b["elements"]]


class FakeVendor:
    def __init__(self, result: Optional[VendorResult] = None):
        self.result = result
        self.asked: list = []
        self.approved: list[ApprovedAnswer] = []
        self.claim = UNCLAIMED

    async def ask(self, asker_ref, question):
        self.asked.append((asker_ref, question))
        return self.result

    def claim_escalation(self, sender, esc):
        return DROPPED if sender != VENDOR else self.claim

    async def send_approved_answer(self, msg):
        self.approved.append(msg)


class FakeClock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def escalation(**kw) -> Escalation:
    base = dict(escalation_id="esc-1", question="What is the shelf life after opening?",
                reason=EscalationReason.LOW_CONFIDENCE, topic=Topic.RND, asker_ref=ASKER_REF)
    base.update(kw)
    return Escalation(**base)


def flagged_escalation(**kw) -> Escalation:
    return escalation(reason=EscalationReason.GUARD_NEEDS_REVIEW, topic=None, draft_answer=None, guard_verdict=None, **kw)


def draft_escalation(**kw) -> Escalation:
    verdict = GuardVerdict(request_id="r1", verdict=Verdict.NEEDS_REVIEW, reason="judge unsure")
    return escalation(reason=EscalationReason.GUARD_NEEDS_REVIEW, draft_answer="Store at 2-8 C for 30 days.",
                      guard_verdict=verdict, **kw)


@dataclass
class Rig:
    core: RouterCore
    slack: FakeSlack
    vendor: FakeVendor
    matcher: FakeMatcher
    clock: FakeClock
    store: Store
    config: Config

    def proposal(self):
        return [p for p in self.slack.posts if p["channel"] == ASK_CHANNEL and "approve_ask" in self.slack.action_ids(p)][-1]

    def expert_post(self, channel):
        return self.slack.in_channel(channel)[0]


@pytest.fixture
def rig():
    slack, vendor, matcher, clock = FakeSlack(), FakeVendor(), FakeMatcher(), FakeClock()
    config, store = make_config(), Store(":memory:")
    core = RouterCore(matcher, vendor, slack, store, config, clock,
                      allowlist=lambda topic: ALLOWLIST.get(topic, set()), lookup_person=DIRECTORY.get)
    return Rig(core, slack, vendor, matcher, clock, store, config)

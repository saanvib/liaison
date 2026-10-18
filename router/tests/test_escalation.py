"""Question flow, proposal and approval (R16, D10, D22, D23, T10). Fakes only."""
from contracts import Topic

from router import texts
from router.store import CANCELLED, PROPOSED, SENT
from router.vendor_client import VendorResult
from router.tests.conftest import (
    ASK_CHANNEL, ASK_TS, ASKER, OTHER, VENDOR, MatchResult, draft_escalation, escalation,
    flagged_escalation,
)


# ---- question in, answer out ----------------------------------------------------------

async def test_ask_command_posts_question_then_answers_in_its_thread(rig):
    rig.vendor.result = VendorResult("answered", "Shelf life is 12 months. [IFU p.4]")
    await rig.core.on_ask_command(ASKER, ASK_CHANNEL, "  shelf life?  ", "https://r")
    question_post, answer_post = rig.slack.posts
    assert question_post["thread_ts"] is None and "shelf life?" in question_post["text"]
    assert rig.vendor.asked == [(f"slack:{ASK_CHANNEL}:{question_post['ts']}:{ASKER}", "shelf life?")]
    assert answer_post["thread_ts"] == question_post["ts"] and answer_post["text"].startswith("Shelf life")


async def test_ask_command_empty_and_not_in_channel(rig):
    await rig.core.on_ask_command(ASKER, ASK_CHANNEL, "   ", "https://r")
    rig.slack.fail_channels.add(ASK_CHANNEL)
    await rig.core.on_ask_command(ASKER, ASK_CHANNEL, "q?", "https://r")
    assert rig.slack.responses == [texts.ASK_USAGE, texts.NOT_IN_CHANNEL]
    assert rig.vendor.asked == []


async def test_bodies_posted_as_they_are(rig):
    for outcome in ("answered", "refused", "retry", "holding"):
        rig.vendor.result = VendorResult(outcome, f"{outcome} body")
        await rig.core.on_slack_question(ASKER, ASK_CHANNEL, ASK_TS, "q")
    assert [p["text"] for p in rig.slack.posts] == ["answered body", "refused body", "retry body", "holding body"]
    assert all(p["thread_ts"] == ASK_TS for p in rig.slack.posts)


async def test_vendor_timeout_tells_the_asker(rig):
    rig.vendor.result = None
    await rig.core.on_slack_question(ASKER, ASK_CHANNEL, ASK_TS, "q")
    assert rig.slack.posts[-1]["text"] == texts.VENDOR_UNREACHABLE


async def test_escalated_without_escalation_tells_the_asker(rig):
    rig.vendor.result = VendorResult("escalated", "", None)
    await rig.core.on_slack_question(ASKER, ASK_CHANNEL, ASK_TS, "q")
    assert rig.slack.posts[-1]["text"] == texts.VENDOR_UNREACHABLE


async def test_escalated_result_makes_a_proposal(rig):
    rig.vendor.result = VendorResult("escalated", "", escalation())
    await rig.core.on_slack_question(ASKER, ASK_CHANNEL, ASK_TS, "q")
    assert rig.store.get("esc-1").state == PROPOSED


# ---- proposal ------------------------------------------------------------------------

async def test_proposal_names_matched_expert_and_sends_nothing(rig):
    await rig.core.on_escalation(escalation())
    p = rig.proposal()
    assert p["thread_ts"] == ASK_TS
    assert "Dr. Rivera" in p["text"] and "Senior Scientist" in p["text"] and "<#C_RD>" in p["text"]
    assert "What is the shelf life after opening?" in p["text"]
    assert rig.slack.action_ids(p) == ["approve_ask", "cancel_ask"]
    assert rig.slack.in_channel("C_RD") == []
    assert rig.matcher.calls == [("What is the shelf life after opening?", Topic.RND)]


async def test_no_match_falls_back_to_topic_channel(rig):
    rig.matcher.result = MatchResult(None, Topic.SALES_OPS, 0.1, True)
    await rig.core.on_escalation(escalation(topic=None))
    assert "the team channel <#C_SALES>" in rig.proposal()["text"]
    await rig.core.on_approve_ask("esc-1", ASKER, ASK_CHANNEL)
    post = rig.expert_post("C_SALES")
    assert "<@" not in post["text"]
    assert rig.store.get("esc-1").topic == "sales_ops"


async def test_flagged_never_calls_matcher_and_uses_fixed_fallback(rig):
    await rig.core.on_escalation(flagged_escalation())
    assert rig.matcher.calls == []
    p = rig.proposal()
    assert "potentially classified" in p["text"] and "<@U_REVIEWER>" in p["text"] and "<#C_REVIEW>" in p["text"]
    rec = rig.store.get("esc-1")
    assert rec.flagged and rec.topic is None and rec.channel_id == "C_REVIEW"
    await rig.core.on_approve_ask("esc-1", ASKER, ASK_CHANNEL)
    post = rig.expert_post("C_REVIEW")
    assert texts.FLAGGED_BANNER in post["text"] and "<@U_REVIEWER>" in post["text"]


async def test_draft_escalation_is_not_flagged(rig):
    await rig.core.on_escalation(draft_escalation())
    assert rig.store.get("esc-1").flagged is False and rig.matcher.calls


async def test_duplicate_escalation_ignored(rig):
    await rig.core.on_escalation(escalation())
    await rig.core.on_escalation(escalation())
    assert len(rig.slack.posts) == 1


async def test_non_slack_asker_ref_dropped(rig):
    await rig.core.on_escalation(escalation(asker_ref="chat:agent1x"))
    assert rig.slack.posts == [] and rig.store.get("esc-1") is None


async def test_vendor_escalation_trust(rig):
    await rig.core.on_vendor_escalation("agent1evil", escalation())
    assert rig.store.get("esc-1") is None
    await rig.core.on_vendor_escalation(VENDOR, escalation())
    assert rig.store.get("esc-1").state == PROPOSED


# ---- clicks --------------------------------------------------------------------------

async def test_only_asker_can_click(rig):
    await rig.core.on_escalation(escalation())
    await rig.core.on_approve_ask("esc-1", OTHER, ASK_CHANNEL)
    await rig.core.on_cancel_ask("esc-1", OTHER, ASK_CHANNEL)
    assert [e["user"] for e in rig.slack.ephemerals] == [OTHER, OTHER]
    assert all(e["text"] == texts.ONLY_ASKER for e in rig.slack.ephemerals)
    assert rig.store.get("esc-1").state == PROPOSED and rig.slack.in_channel("C_RD") == []


async def test_approve_posts_to_expert_once(rig):
    await rig.core.on_escalation(escalation())
    await rig.core.on_approve_ask("esc-1", ASKER, ASK_CHANNEL)
    await rig.core.on_approve_ask("esc-1", ASKER, ASK_CHANNEL)  # double click
    posts = rig.slack.in_channel("C_RD")
    assert len(posts) == 1
    assert "<@U_RND_1>" in posts[0]["text"] and "shelf life" in posts[0]["text"]
    rec = rig.store.get("esc-1")
    assert rec.state == SENT and rec.thread_ts == posts[0]["ts"] and rec.sent_at == rig.clock.t
    assert rig.slack.updates and rig.slack.action_ids(rig.slack.updates[-1]) == []  # buttons removed


async def test_cancel_posts_nothing_to_expert(rig):
    await rig.core.on_escalation(escalation())
    await rig.core.on_cancel_ask("esc-1", ASKER, ASK_CHANNEL)
    await rig.core.on_approve_ask("esc-1", ASKER, ASK_CHANNEL)  # too late
    assert rig.store.get("esc-1").state == CANCELLED
    assert rig.slack.in_channel("C_RD") == []
    assert rig.slack.updates[-1]["text"] == texts.CANCELLED


async def test_expert_channel_failure_reverts_to_proposed(rig):
    await rig.core.on_escalation(escalation())
    rig.slack.fail_channels.add("C_RD")
    await rig.core.on_approve_ask("esc-1", ASKER, ASK_CHANNEL)
    assert rig.store.get("esc-1").state == PROPOSED
    assert rig.slack.posts[-1]["text"] == texts.EXPERT_CHANNEL_FAILED


async def test_draft_case_shows_two_buttons_after_approval(rig):
    await rig.core.on_escalation(draft_escalation())
    assert rig.slack.in_channel("C_RD") == []
    await rig.core.on_approve_ask("esc-1", ASKER, ASK_CHANNEL)
    post = rig.expert_post("C_RD")
    assert rig.slack.action_ids(post) == ["approve_draft", "edit_draft"]
    assert any("Store at 2-8 C" in b["text"]["text"] for b in post["blocks"] if b["type"] == "section")
    assert any("judge unsure" in b["text"]["text"] for b in post["blocks"] if b["type"] == "section")

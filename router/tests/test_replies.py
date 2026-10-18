"""Expert replies, allowlist, reminder, ApprovedAnswer (D8, D11, D17, D18, D23, A4). Fake clock."""
from contracts import Topic

from router import texts
from router.store import ANSWERED, SENT
from router.tests.conftest import (
    ASK_CHANNEL, ASK_TS, ASKER, ASKER_REF, OTHER, draft_escalation, escalation, flagged_escalation, needs_pr0,
)


async def sent(rig, esc):
    await rig.core.on_escalation(esc)
    await rig.core.on_approve_ask(esc.escalation_id, ASKER, ASK_CHANNEL)
    rec = rig.store.get(esc.escalation_id)
    assert rec.state == SENT
    return rec


def asker_posts(rig):
    return [p for p in rig.slack.posts if p["channel"] == ASK_CHANNEL and p["thread_ts"] == ASK_TS]


async def test_first_reply_wins(rig):
    rec = await sent(rig, escalation())
    await rig.core.on_thread_reply("C_RD", rec.thread_ts, "U_RND_1", "30 days at 2-8 C.")
    await rig.core.on_thread_reply("C_RD", rec.thread_ts, "U_RND_2", "Actually 14 days.")
    answers = [p for p in asker_posts(rig) if p["text"].startswith("*Answer from")]
    assert len(answers) == 1
    assert answers[0]["text"] == texts.answer_for_asker("Dr. Rivera", "Senior Scientist, Assay Development", "30 days at 2-8 C.")
    assert rig.store.get("esc-1").state == ANSWERED
    assert len(rig.vendor.approved) <= 1


async def test_disallowed_user_ignored_with_ephemeral(rig):
    rec = await sent(rig, escalation())
    await rig.core.on_thread_reply("C_RD", rec.thread_ts, OTHER, "I think 1 year")
    assert rig.slack.ephemerals == [dict(channel="C_RD", user=OTHER, text=texts.NOT_ALLOWED, thread_ts=rec.thread_ts)]
    assert rig.store.get("esc-1").state == SENT and rig.vendor.approved == []


async def test_allowlisted_colleague_can_answer_with_directory_name(rig):
    rec = await sent(rig, escalation())
    await rig.core.on_thread_reply("C_RD", rec.thread_ts, "U_RND_2", "30 days.")
    assert "Answer from Dr. Natarajan, Application Scientist" in asker_posts(rig)[-1]["text"]


async def test_reply_before_send_or_in_other_thread_ignored(rig):
    await rig.core.on_escalation(escalation())  # proposed, not sent
    await rig.core.on_thread_reply("C_RD", "123.456", "U_RND_1", "answer")
    assert rig.store.get("esc-1").state == "proposed" and rig.slack.ephemerals == []


async def test_one_reminder_only(rig):
    rec = await sent(rig, escalation())
    rig.clock.t += 899
    await rig.core.tick()
    assert not [p for p in rig.slack.posts if "Reminder" in p["text"]]
    rig.clock.t += 2
    await rig.core.tick()
    rig.clock.t += 5000
    await rig.core.tick()
    reminders = [p for p in rig.slack.posts if "Reminder" in p["text"]]
    assert len(reminders) == 1
    assert reminders[0]["channel"] == "C_RD" and reminders[0]["thread_ts"] == rec.thread_ts
    assert "<@U_RND_1>" in reminders[0]["text"]
    assert rig.store.get("esc-1").reminded is True


async def test_no_reminder_after_answer(rig):
    rec = await sent(rig, escalation())
    await rig.core.on_thread_reply("C_RD", rec.thread_ts, "U_RND_1", "done")
    rig.clock.t += 10_000
    await rig.core.tick()
    assert not [p for p in rig.slack.posts if "Reminder" in p["text"]]


@needs_pr0
async def test_approved_answer_fields(rig):
    rec = await sent(rig, escalation())
    await rig.core.on_thread_reply("C_RD", rec.thread_ts, "U_RND_1", "30 days at 2-8 C.")
    (msg,) = rig.vendor.approved
    assert msg.escalation_id == "esc-1" and msg.question == "What is the shelf life after opening?"
    assert msg.answer == "30 days at 2-8 C." and msg.topic == Topic.RND
    assert msg.approved_by == "U_RND_1|Dr. Rivera"
    assert msg.guard_verdict is None and msg.citations == [] and msg.asker_ref == ASKER_REF


async def test_flagged_answer_returned_but_no_approved_answer(rig):
    rec = await sent(rig, flagged_escalation())
    await rig.core.on_thread_reply("C_REVIEW", rec.thread_ts, "U_RND_1", "not the reviewer")  # topic allowlist doesn't apply
    assert rig.store.get("esc-1").state == SENT
    await rig.core.on_thread_reply("C_REVIEW", rec.thread_ts, "U_REVIEWER", "Handled offline.")
    assert "Answer from Pat Reviewer, Compliance Lead" in asker_posts(rig)[-1]["text"]
    assert rig.vendor.approved == []


@needs_pr0
async def test_draft_approve_as_is(rig):
    await sent(rig, draft_escalation())
    await rig.core.on_approve_draft("esc-1", OTHER, "C_RD")
    assert rig.slack.ephemerals[-1]["text"] == texts.NOT_ALLOWED
    await rig.core.on_approve_draft("esc-1", "U_RND_1", "C_RD")
    (msg,) = rig.vendor.approved
    assert msg.answer == "Store at 2-8 C for 30 days." and msg.approved_by == "U_RND_1|Dr. Rivera"
    await rig.core.on_approve_draft("esc-1", "U_RND_1", "C_RD")  # second click
    assert rig.slack.ephemerals[-1]["text"] == texts.ALREADY_DONE and len(rig.vendor.approved) == 1


@needs_pr0
async def test_draft_edit_stores_typed_text(rig):
    await sent(rig, draft_escalation())
    await rig.core.on_edit_draft("esc-1", "U_RND_1", "C_RD", "trigger-1")
    (modal,) = rig.slack.modals
    assert modal["trigger_id"] == "trigger-1"
    assert modal["view"]["private_metadata"] == "esc-1"
    assert modal["view"]["blocks"][0]["element"]["initial_value"] == "Store at 2-8 C for 30 days."
    await rig.core.on_edit_submit("esc-1", OTHER, "hijack")  # not allowed: ignored
    await rig.core.on_edit_submit("esc-1", "U_RND_1", "Store at 2-8 C; use within 14 days.")
    (msg,) = rig.vendor.approved
    assert msg.answer == "Store at 2-8 C; use within 14 days."
    assert "use within 14 days" in asker_posts(rig)[-1]["text"]


async def test_answer_reaches_asker_even_if_vendor_send_fails(rig, caplog):
    async def boom(msg):
        raise RuntimeError("mailbox down")

    rig.vendor.send_approved_answer = boom
    rec = await sent(rig, escalation())
    await rig.core.on_thread_reply("C_RD", rec.thread_ts, "U_RND_1", "30 days")
    assert "30 days" in asker_posts(rig)[-1]["text"]
    assert "could not send ApprovedAnswer" in caplog.text

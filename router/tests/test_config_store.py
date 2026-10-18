import pytest

from contracts import Topic

from router.config import Config
from router.store import ANSWERED, PROPOSED, SENT, EscalationRecord, Store
from router.tests.conftest import make_config


def test_config_from_env_defaults_and_channels():
    c = make_config()
    assert c.vendor_wait_s == 30 and c.reminder_after_s == 900
    assert c.topic_channel(Topic.SALES_OPS) == "C_SALES"
    assert "C_REVIEW" in c.expert_channels()


def test_config_missing_vars_exits():
    with pytest.raises(SystemExit, match="SLACK_BOT_TOKEN"):
        Config.from_env({})


def record(**kw) -> EscalationRecord:
    base = dict(escalation_id="e1", asker_ref="slack:C:1.2:U", question="q", topic="rd", flagged=False,
                has_draft=False, draft_answer=None, state=PROPOSED, proposed_expert_id=None, recipient_user_id=None,
                recipient_name=None, recipient_role=None, channel_id="C_RD", thread_ts=None, created_at=1.0)
    base.update(kw)
    return EscalationRecord(**base)


def test_store_roundtrip_and_duplicate_add():
    s = Store()
    assert s.add(record(flagged=True)) is True
    assert s.add(record()) is False
    got = s.get("e1")
    assert got.flagged is True and got.reminded is False and got.state == PROPOSED


def test_transition_is_compare_and_set():
    s = Store()
    s.add(record(state=SENT, thread_ts="9.9"))
    assert s.transition("e1", [SENT], ANSWERED) is True
    assert s.transition("e1", [SENT], ANSWERED) is False
    assert s.find_by_thread("C_RD", "9.9").state == ANSWERED


def test_due_reminders_and_mark_once():
    s = Store()
    s.add(record(state=SENT, sent_at=100.0, thread_ts="9.9"))
    assert [r.escalation_id for r in s.due_reminders(100.0)] == ["e1"]
    assert s.due_reminders(99.0) == []
    assert s.mark_reminded("e1") is True
    assert s.mark_reminded("e1") is False
    assert s.due_reminders(1000.0) == []


def test_update_rejects_unknown_column():
    s = Store()
    s.add(record())
    with pytest.raises(ValueError):
        s.update("e1", nope=1)

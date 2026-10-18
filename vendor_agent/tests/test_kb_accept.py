from datetime import datetime, timezone

import pytest

from contracts import ApprovedAnswer, Escalation, EscalationReason, GuardVerdict, Topic, Verdict
from vendor_agent.clients import OfflineEmbedder
from vendor_agent.config import Config
from vendor_agent.kb import KnowledgeBase
from vendor_agent.retrieval import DictStorage

ROUTER = "router1"
FIXED_NOW = datetime(2024, 1, 15, tzinfo=timezone.utc)


def make_kb(storage=None) -> KnowledgeBase:
    config = Config(
        router_address=ROUTER, guard_address="guard1", ingest_address="ingest1",
        approver_allowlist=frozenset({"U1"}),
    )
    return KnowledgeBase(storage or DictStorage(), OfflineEmbedder(), config, clock=lambda: FIXED_NOW)


def pend(kb: KnowledgeBase, escalation_id: str, flagged: bool = False) -> None:
    kb.add_pending(
        Escalation(escalation_id=escalation_id, question="what is the shelf life?", reason=EscalationReason.LOW_CONFIDENCE),
        flagged=flagged,
    )


def answer(escalation_id: str, guard_verdict=None, approved_by="guard") -> ApprovedAnswer:
    return ApprovedAnswer(
        escalation_id=escalation_id, question="what is the shelf life?", answer="18 months",
        topic=Topic.RND, approved_by=approved_by, guard_verdict=guard_verdict,
    )


APPROVED = GuardVerdict(request_id="r1", verdict=Verdict.APPROVED, reason="ok")
NEEDS_REVIEW = GuardVerdict(request_id="r1", verdict=Verdict.NEEDS_REVIEW, reason="unsure")
BLOCKED = GuardVerdict(request_id="r1", verdict=Verdict.BLOCKED, reason="patient data")


@pytest.mark.parametrize(
    "escalation_id, flagged, sender, guard_verdict, approved_by, expect_accepted, expect_reason",
    [
        ("e-approved", False, ROUTER, APPROVED, "guard", True, "accepted"),
        ("e-allowlisted", False, ROUTER, None, "U1|Jane Doe", True, "accepted"),
        ("e-not-allowlisted", False, ROUTER, None, "U2|Bob", False, "approver_not_allowed"),
        ("e-needs-review", False, ROUTER, NEEDS_REVIEW, "guard", False, "bad_verdict"),
        ("e-blocked", False, ROUTER, BLOCKED, "guard", False, "bad_verdict"),
        ("e-wrong-sender", False, "not-the-router", APPROVED, "guard", False, "untrusted_sender"),
        ("e-malformed", False, ROUTER, None, "no-pipe-here", False, "approver_not_allowed"),
    ],
)
def test_accept_table(escalation_id, flagged, sender, guard_verdict, approved_by, expect_accepted, expect_reason):
    kb = make_kb()
    pend(kb, escalation_id, flagged=flagged)
    result = kb.accept(answer(escalation_id, guard_verdict, approved_by), sender=sender)
    assert result.accepted is expect_accepted
    assert result.reason == expect_reason


def test_accept_unknown_escalation_rejected():
    kb = make_kb()
    result = kb.accept(answer("never-pended", APPROVED, "guard"), sender=ROUTER)
    assert (result.accepted, result.reason) == (False, "unknown_escalation")


def test_accept_flagged_pending_rejected():
    kb = make_kb()
    pend(kb, "e-flagged", flagged=True)
    result = kb.accept(answer("e-flagged", APPROVED, "guard"), sender=ROUTER)
    assert (result.accepted, result.reason) == (False, "flagged_not_stored")


def test_accept_duplicate_is_idempotent_noop():
    kb = make_kb()
    pend(kb, "e-dup")
    first = kb.accept(answer("e-dup", APPROVED, "guard"), sender=ROUTER)
    assert (first.accepted, first.reason) == (True, "accepted")
    entries_after_first = kb.storage.get("vendor_agent:kb_entries")
    assert len(entries_after_first) == 1

    second = kb.accept(answer("e-dup", APPROVED, "guard"), sender=ROUTER)
    assert (second.accepted, second.reason) == (True, "duplicate")
    assert len(kb.storage.get("vendor_agent:kb_entries")) == 1  # nothing written again


class _RaisingStorage(DictStorage):
    def set(self, key, value):
        if key == "vendor_agent:kb_entries":
            raise RuntimeError("storage unavailable")
        super().set(key, value)


def test_accept_storage_error_returns_storage_error():
    kb = make_kb(storage=_RaisingStorage())
    pend(kb, "e-storage-error")
    result = kb.accept(answer("e-storage-error", APPROVED, "guard"), sender=ROUTER)
    assert (result.accepted, result.reason) == (False, "storage_error")


def test_lookup_hit_above_threshold_and_miss_below():
    kb = make_kb()
    pend(kb, "e1")
    kb.accept(answer("e1", APPROVED, "guard"), sender=ROUTER)

    hit = kb.lookup("what is the shelf life?")
    assert hit is not None
    assert hit.answer == "18 months"

    miss = kb.lookup("completely unrelated question about kayaking")
    assert miss is None


def test_attribution_note_format():
    kb = make_kb()
    pend(kb, "e1")
    kb.accept(answer("e1", None, "U1|Jane Doe"), sender=ROUTER)
    entry = kb.lookup("what is the shelf life?")
    assert kb.attribution_note(entry) == "Expert answer (Jane Doe, rd, 2024-01-15)"

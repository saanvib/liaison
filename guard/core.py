"""GuardCore: picks a mode from the request (A6) and layers the checks (D3).

  screen  (empty draft):                 screen rules on the question only.
  draft   (VENDOR_AGENT, non-empty):     rules -> judge -> grounding.
  egress  (EXPERT, non-empty):           rules -> judge. No grounding.

A rule hit is final: the judge is not consulted. The judge can only keep or
tighten an approved verdict. Unexpected errors fail closed (needs_review).
Synchronous and pure apart from the injected LLM clients.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

from contracts import GuardCategory, GuardRequest, GuardVerdict, Source, Topic, Verdict

from guard.grounding import Grounding
from guard.judge import JUDGE_UNAVAILABLE, Judge
from guard.rules import Rule, RuleHit, load_policy, load_rules, screen_categories

log = logging.getLogger(__name__)

_STRICTNESS = {Verdict.APPROVED: 0, Verdict.NEEDS_REVIEW: 1, Verdict.BLOCKED: 2}


class Mode(str, Enum):
    SCREEN = "screen"
    DRAFT = "draft"
    EGRESS = "egress"


def detect_mode(req: GuardRequest) -> Mode:
    if not req.draft_answer.strip():
        return Mode.SCREEN
    if req.source == Source.EXPERT:
        return Mode.EGRESS
    return Mode.DRAFT


def _reroute(category: GuardCategory) -> Optional[Topic]:
    return Topic.SALES_OPS if category == GuardCategory.PRICING_CONTRACT else None


class GuardCore:
    def __init__(
        self,
        rules: Optional[list[Rule]] = None,
        judge: Optional[Judge] = None,
        grounding: Optional[Grounding] = None,
        policy: Optional[dict] = None,
    ):
        """`judge=None` means no judge is configured: draft and egress checks that
        pass the rules then return needs_review ("judge unavailable"), never approved."""
        policy = policy if policy is not None else load_policy()
        self.rules = rules if rules is not None else load_rules(policy)
        self.screen_categories = screen_categories(policy)
        self.judge = judge
        self.grounding = grounding if grounding is not None else Grounding()

    def check(self, req: GuardRequest) -> GuardVerdict:
        try:
            return self._check(req)
        except Exception:
            log.exception("guard check failed for request %s", req.request_id)
            return self._verdict(req, Verdict.NEEDS_REVIEW, GuardCategory.NONE, "Guard error; needs human review.")

    def _check(self, req: GuardRequest) -> GuardVerdict:
        mode = detect_mode(req)

        if mode == Mode.SCREEN:
            for rule in self.rules:
                if rule.category in self.screen_categories:
                    hit = rule.check(req)
                    if hit:
                        return self._from_hit(req, hit)
            return self._verdict(req, Verdict.APPROVED, GuardCategory.NONE, "Screen passed: no patient data found.")

        for rule in self.rules:
            hit = rule.check(req)
            if hit:
                return self._from_hit(req, hit)

        judged = self.judge.assess(req) if self.judge is not None else None
        if judged is None:
            return self._verdict(req, Verdict.NEEDS_REVIEW, GuardCategory.NONE, JUDGE_UNAVAILABLE)
        if _STRICTNESS[judged.verdict] > _STRICTNESS[Verdict.APPROVED]:
            return self._verdict(req, judged.verdict, judged.category, judged.reason)

        if mode == Mode.EGRESS:
            return self._verdict(req, Verdict.APPROVED, GuardCategory.NONE, "Passed rules and judge.")

        grounded = self.grounding.check(req)
        if not grounded.grounded:
            return self._verdict(req, Verdict.NEEDS_REVIEW, GuardCategory.UNGROUNDED, grounded.reason)
        return self._verdict(req, Verdict.APPROVED, GuardCategory.NONE, "Passed rules, judge and grounding.")

    def _from_hit(self, req: GuardRequest, hit: RuleHit) -> GuardVerdict:
        return GuardVerdict(
            request_id=req.request_id,
            verdict=hit.verdict,
            category=hit.category,
            reason=hit.reason,
            reroute_topic=hit.reroute_topic or _reroute(hit.category),
        )

    def _verdict(self, req: GuardRequest, verdict: Verdict, category: GuardCategory, reason: str) -> GuardVerdict:
        return GuardVerdict(
            request_id=req.request_id,
            verdict=verdict,
            category=category,
            reason=reason or "No reason given.",
            reroute_topic=_reroute(category),
        )

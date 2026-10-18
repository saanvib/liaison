"""Deterministic Guard rules, one class per blocker category.

Patterns, verdicts and reason templates live in policy.json. A rule hit is
final: GuardCore returns it without consulting the judge (D3 layering).
Reasons say what kind of thing was found, never the matched text itself.
Standard library only, so the hosted agent needs no extra packages (A11).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from contracts import GuardCategory, GuardRequest, Topic, Verdict

# Resolved in load_policy, not at import: hosted Agentverse loads modules without __file__
# and passes the embedded policy instead (guard/hosted/entry.py).
POLICY_FILENAME = "policy.json"

_FIELD_NAMES = {"question": "question", "draft_answer": "draft"}


@dataclass(frozen=True)
class RuleHit:
    category: GuardCategory
    verdict: Verdict
    reason: str
    reroute_topic: Optional[Topic] = None


@dataclass(frozen=True)
class _Pattern:
    label: str
    regex: re.Pattern
    context: Optional[re.Pattern] = None

    def matches(self, text: str) -> bool:
        if not self.regex.search(text):
            return False
        return self.context is None or bool(self.context.search(text))


def _compile(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


class Rule:
    """Base rule: scans the configured request fields for any policy pattern."""

    category: GuardCategory

    def __init__(self, spec: dict):
        self.verdict = Verdict(spec["verdict"])
        self.reason_template: str = spec["reason"]
        self.scan: list[str] = spec.get("scan", ["question", "draft_answer"])
        topic = spec.get("reroute_topic")
        self.reroute_topic = Topic(topic) if topic else None
        self.strip = [_compile(p) for p in spec.get("strip", [])]
        self.patterns = [
            _Pattern(
                label=p["label"],
                regex=_compile(p["pattern"]),
                context=_compile(p["context"]) if p.get("context") else None,
            )
            for p in spec["patterns"]
        ]

    def check(self, req: GuardRequest) -> Optional[RuleHit]:
        for field in self.scan:
            text = getattr(req, field, "") or ""
            for strip in self.strip:
                text = strip.sub(" ", text)
            for pattern in self.patterns:
                if pattern.matches(text):
                    return RuleHit(
                        category=self.category,
                        verdict=self.verdict,
                        reason=self.reason_template.format(
                            field=_FIELD_NAMES.get(field, field), label=pattern.label
                        ),
                        reroute_topic=self.reroute_topic,
                    )
        return None


class PatientDataRule(Rule):
    category = GuardCategory.PATIENT_DATA


class RuoClinicalUseRule(Rule):
    category = GuardCategory.RUO_CLINICAL_USE


class OffLabelRule(Rule):
    category = GuardCategory.OFF_LABEL


class ComparativeClaimRule(Rule):
    category = GuardCategory.COMPARATIVE_CLAIM


class PricingContractRule(Rule):
    category = GuardCategory.PRICING_CONTRACT


class RoadmapRule(Rule):
    category = GuardCategory.ROADMAP


# Order matters only for which category is reported when several hit:
# patient data first, since it also stops the request from going anywhere.
RULE_CLASSES: list[type[Rule]] = [
    PatientDataRule,
    RuoClinicalUseRule,
    OffLabelRule,
    ComparativeClaimRule,
    PricingContractRule,
    RoadmapRule,
]


def load_policy(path: Optional[Path] = None) -> dict:
    path = path or Path(__file__).with_name(POLICY_FILENAME)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_rules(policy: Optional[dict] = None) -> list[Rule]:
    """All rules, in RULE_CLASSES order, built from the policy."""
    policy = policy if policy is not None else load_policy()
    categories = policy["categories"]
    return [cls(categories[cls.category.value]) for cls in RULE_CLASSES]


def screen_categories(policy: Optional[dict] = None) -> set[GuardCategory]:
    """Categories run in screen mode (A6); configurable via policy 'screen_rules'."""
    policy = policy if policy is not None else load_policy()
    return {GuardCategory(c) for c in policy.get("screen_rules", ["patient_data"])}

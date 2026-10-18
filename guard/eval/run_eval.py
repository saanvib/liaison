"""Pass/fail eval of the Compliance Guard on the labeled set (D13).

    python -m guard.eval.run_eval                # offline: approving fake judge, overlap grounding
    python -m guard.eval.run_eval --llm real     # real judge + LLM grounding (needs LLM_API_KEY)
    python -m guard.eval.run_eval --policy path/to/policy.json

Per category: recall = rows flagged with that category / rows expected with it;
precision = rows correctly flagged with that category / rows flagged with it.
"Flagged" means verdict blocked or needs_review. Overall precision = flagged
rows that were expected to be flagged / all flagged rows.

Bar: every blocker category has recall 100% and overall precision >= 80%.
Exit 0 if the bar is met, 1 if not, 2 on a usage or setup error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from contracts import Citation, GuardCategory, GuardRequest, Source, Verdict

from guard.core import GuardCore
from guard.grounding import Grounding
from guard.judge import JUDGE_UNAVAILABLE, Judge, JudgeResult
from guard.rules import load_policy

LABELED_SET = Path(__file__).with_name("labeled_set.jsonl")

BLOCKER_CATEGORIES = [
    GuardCategory.PATIENT_DATA,
    GuardCategory.RUO_CLINICAL_USE,
    GuardCategory.OFF_LABEL,
    GuardCategory.COMPARATIVE_CLAIM,
    GuardCategory.PRICING_CONTRACT,
    GuardCategory.ROADMAP,
]
REPORTED_CATEGORIES = BLOCKER_CATEGORIES + [GuardCategory.UNGROUNDED]
RECALL_BAR = 1.0
PRECISION_BAR = 0.8


class ApprovingJudge:
    """Offline stand-in: approves everything, so all recall comes from the deterministic rules."""

    def assess(self, req: GuardRequest) -> JudgeResult:
        return JudgeResult(Verdict.APPROVED, GuardCategory.NONE, "fake judge: approved")


def load_rows(path: Path = LABELED_SET) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def to_request(row: dict) -> GuardRequest:
    return GuardRequest(
        request_id=row["id"],
        source=Source(row["source"]),
        question=row["question"],
        draft_answer=row["draft_answer"],
        citations=[Citation(**c) for c in row.get("citations", [])],
    )


def _flagged(verdict: Verdict) -> bool:
    return verdict != Verdict.APPROVED


@dataclass
class CategoryStats:
    expected: int = 0
    flagged: int = 0
    correct: int = 0

    @property
    def recall(self) -> Optional[float]:
        return self.correct / self.expected if self.expected else None

    @property
    def precision(self) -> Optional[float]:
        return self.correct / self.flagged if self.flagged else None


@dataclass
class Report:
    rows: int = 0
    stats: dict = field(default_factory=lambda: {c: CategoryStats() for c in REPORTED_CATEGORIES})
    flagged: int = 0
    flagged_expected: int = 0
    judge_unavailable: int = 0  # rows whose verdict came from a failed judge call
    mismatches: list = field(default_factory=list)  # (id, expected, got)

    @property
    def overall_precision(self) -> Optional[float]:
        return self.flagged_expected / self.flagged if self.flagged else None

    def failures(self) -> list[str]:
        out = []
        for c in BLOCKER_CATEGORIES:
            s = self.stats[c]
            if s.expected == 0:
                out.append(f"{c.value}: no labeled positives")
            elif s.recall < RECALL_BAR:
                out.append(f"{c.value}: recall {s.recall:.0%} < {RECALL_BAR:.0%}")
        p = self.overall_precision
        if p is not None and p < PRECISION_BAR:
            out.append(f"overall precision {p:.0%} < {PRECISION_BAR:.0%}")
        return out

    @property
    def passed(self) -> bool:
        return not self.failures()


def evaluate(core: GuardCore, rows: list[dict]) -> Report:
    report = Report(rows=len(rows))
    for row in rows:
        exp_verdict = Verdict(row["expected_verdict"])
        exp_category = GuardCategory(row["expected_category"])
        got = core.check(to_request(row))
        got_flag = _flagged(got.verdict)
        report.judge_unavailable += got.reason == JUDGE_UNAVAILABLE

        if exp_category in report.stats:
            report.stats[exp_category].expected += 1
        if got_flag and got.category in report.stats:
            report.stats[got.category].flagged += 1
            if got.category == exp_category and _flagged(exp_verdict):
                report.stats[got.category].correct += 1
        if got_flag:
            report.flagged += 1
            report.flagged_expected += _flagged(exp_verdict)
        if (got.verdict, got.category) != (exp_verdict, exp_category):
            report.mismatches.append(
                (row["id"], f"{exp_verdict.value}/{exp_category.value}", f"{got.verdict.value}/{got.category.value}")
            )
    return report


def _pct(x: Optional[float]) -> str:
    return "  n/a" if x is None else f"{x:5.0%}"


def format_report(report: Report, llm_mode: str) -> str:
    lines = [
        f"Guard eval: {report.rows} rows, judge={llm_mode}",
        "",
        f"{'category':<20}{'expected':>9}{'flagged':>9}{'recall':>8}{'precision':>11}",
    ]
    for c in REPORTED_CATEGORIES:
        s = report.stats[c]
        marker = "" if c in BLOCKER_CATEGORIES else "  (not a blocker)"
        lines.append(f"{c.value:<20}{s.expected:>9}{s.flagged:>9}{_pct(s.recall):>8}{_pct(s.precision):>11}{marker}")
    lines.append(f"{'overall':<20}{'':>9}{report.flagged:>9}{'':>8}{_pct(report.overall_precision):>11}")
    lines += [
        "",
        f"Bar: recall {RECALL_BAR:.0%} for every blocker category, overall precision >= {PRECISION_BAR:.0%}.",
        "Note: the basis is about 5 positives per category (estimated), so one miss moves recall by ~20 points.",
    ]
    if report.mismatches:
        lines += ["", "Rows where the verdict/category differed from the label:"]
        lines += [f"  {rid}: expected {exp}, got {got}" for rid, exp, got in report.mismatches]
    failures = report.failures()
    lines += ["", "PASS" if not failures else "FAIL: " + "; ".join(failures)]
    return "\n".join(lines)


def build_core(llm_mode: str, policy: dict) -> GuardCore:
    if llm_mode == "fake":
        return GuardCore(judge=ApprovingJudge(), grounding=Grounding(), policy=policy)
    from guard.llm import AnthropicLLM

    llm = AnthropicLLM()
    return GuardCore(judge=Judge(llm), grounding=Grounding(llm), policy=policy)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--llm", choices=["fake", "real"], default="fake")
    parser.add_argument("--policy", type=Path, default=None, help="policy JSON (default: guard/policy.json)")
    parser.add_argument("--labeled-set", type=Path, default=LABELED_SET)
    args = parser.parse_args(argv)

    if args.llm == "real" and not os.environ.get("LLM_API_KEY"):
        print("--llm real needs LLM_API_KEY in the environment.", file=sys.stderr)
        return 2
    policy = load_policy(args.policy) if args.policy else load_policy()
    core = build_core(args.llm, policy)
    report = evaluate(core, load_rows(args.labeled_set))
    print(format_report(report, args.llm))
    if args.llm == "real" and report.judge_unavailable:
        why = getattr(core.judge, "last_error", None) or "unknown"
        print(
            f"\nFAIL: the real judge was unavailable on {report.judge_unavailable} row(s), so it was not "
            f"actually tested (last error: {why})."
        )
        return 1
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())

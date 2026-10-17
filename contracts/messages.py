"""Shared messages between Vendor Agent, Compliance Guard, and Router/Slack bot.

Flow:
  Vendor Agent --GuardRequest--> Guard --GuardVerdict--> Vendor Agent
  Vendor Agent --Escalation--> Router --(expert reply)--> GuardRequest --> Guard
  Router --ApprovedAnswer--> Vendor Agent (written to KB)
  Router --IngestDocument--> Vendor Agent (document drop in #liaison-docs)

All messages are uAgents Models, so they can be sent directly between agents.
Every message carries a correlation id; replies echo the id of the message they answer.
"""
from enum import Enum
from typing import List, Optional

from uagents import Model


class Verdict(str, Enum):
    APPROVED = "approved"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"


class GuardCategory(str, Enum):
    """Why the Guard flagged a draft. NONE when approved."""
    NONE = "none"
    RUO_CLINICAL_USE = "ruo_clinical_use"          # clinical/diagnostic use for an RUO product
    OFF_LABEL = "off_label"
    COMPARATIVE_CLAIM = "comparative_claim"        # claims against competitors
    PRICING_CONTRACT = "pricing_contract"          # pricing or contract commitments -> sales
    ROADMAP = "roadmap"                            # future features
    PATIENT_DATA = "patient_data"
    UNGROUNDED = "ungrounded"                      # draft not supported by cited sources


class Topic(str, Enum):
    """Expert-routing topics; the Router maps each to a Slack channel."""
    RND = "rd"
    QA_RA = "qa_ra"
    ENGINEERING = "engineering"
    SALES_OPS = "sales_ops"


class Source(str, Enum):
    """Who wrote the draft being checked."""
    VENDOR_AGENT = "vendor_agent"
    EXPERT = "expert"


class EscalationReason(str, Enum):
    LOW_CONFIDENCE = "low_confidence"
    GUARD_BLOCKED = "guard_blocked"
    GUARD_NEEDS_REVIEW = "guard_needs_review"


class Citation(Model):
    doc_id: str
    doc_title: str
    snippet: str
    location: Optional[str] = None   # page / section


class GuardRequest(Model):
    request_id: str
    source: Source
    question: str
    draft_answer: str
    citations: List[Citation] = []
    confidence: Optional[float] = None   # 0..1, from the Vendor Agent's retrieval
    escalation_id: Optional[str] = None  # set when checking an expert's answer


class GuardVerdict(Model):
    request_id: str                      # echoes GuardRequest.request_id
    verdict: Verdict
    category: GuardCategory = GuardCategory.NONE
    reason: str                          # always present, human-readable
    reroute_topic: Optional[Topic] = None  # e.g. SALES_OPS for pricing
    revised_answer: Optional[str] = None   # optional safe rewrite suggested by the Guard


class Escalation(Model):
    escalation_id: str
    question: str
    reason: EscalationReason
    topic: Optional[Topic] = None        # None = Router classifies
    draft_answer: Optional[str] = None   # what the Vendor Agent would have said
    citations: List[Citation] = []
    guard_verdict: Optional[GuardVerdict] = None
    asker_ref: Optional[str] = None      # opaque handle (e.g. ASI:One session) to deliver the final answer


class ApprovedAnswer(Model):
    escalation_id: str
    question: str
    answer: str
    topic: Topic
    approved_by: str                     # expert id/name, or "guard" if auto-approved
    guard_verdict: GuardVerdict          # the approving verdict, for audit
    citations: List[Citation] = []
    asker_ref: Optional[str] = None


class IngestDocument(Model):
    doc_id: str
    title: str
    doc_type: str                        # datasheet | ifu | validation_report | faq | other
    text: str                            # extracted plain text
    filename: Optional[str] = None
    uploaded_by: Optional[str] = None
    source: str = "slack"                # slack | cli | api

"""Vendor Agent configuration, loaded from env (A9, A10, A12)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Mapping, Optional

from contracts import Topic

DEFAULT_LLM_MODEL = "gemini-2.5-flash"
DEFAULT_GUARD_TIMEOUT_S = 10.0
DEFAULT_KB_MATCH_THRESHOLD = 0.85
DEFAULT_LOW_CONFIDENCE = 0.5

DEFAULT_TEAM_ROLES: dict[Topic, str] = {
    Topic.RND: "our R&D team",
    Topic.QA_RA: "our Quality and Regulatory team",
    Topic.ENGINEERING: "our Engineering team",
    Topic.SALES_OPS: "our Sales Operations team",
}


@dataclass(frozen=True)
class Config:
    router_address: str
    guard_address: str
    ingest_address: str
    approver_allowlist: frozenset[str]
    guard_timeout_s: float = DEFAULT_GUARD_TIMEOUT_S
    kb_match_threshold: float = DEFAULT_KB_MATCH_THRESHOLD
    low_confidence: float = DEFAULT_LOW_CONFIDENCE
    llm_api_key: Optional[str] = None
    llm_model: str = DEFAULT_LLM_MODEL
    embeddings_url: Optional[str] = None
    embeddings_api_key: Optional[str] = None
    embeddings_model: Optional[str] = None
    team_roles: Mapping[Topic, str] = field(default_factory=lambda: dict(DEFAULT_TEAM_ROLES))

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Config":
        env = os.environ if env is None else env
        allowlist = frozenset(
            s.strip() for s in env.get("APPROVER_ALLOWLIST", "").split(",") if s.strip()
        )
        team_roles = dict(DEFAULT_TEAM_ROLES)
        raw_roles = env.get("TEAM_ROLES")
        if raw_roles:
            team_roles.update({Topic(k): v for k, v in json.loads(raw_roles).items()})
        return cls(
            router_address=env["ROUTER_ADDRESS"],
            guard_address=env["GUARD_ADDRESS"],
            ingest_address=env["INGEST_ADDRESS"],
            approver_allowlist=allowlist,
            guard_timeout_s=float(env.get("GUARD_TIMEOUT_S", DEFAULT_GUARD_TIMEOUT_S)),
            kb_match_threshold=float(env.get("KB_MATCH_THRESHOLD", DEFAULT_KB_MATCH_THRESHOLD)),
            low_confidence=float(env.get("LOW_CONFIDENCE", DEFAULT_LOW_CONFIDENCE)),
            llm_api_key=env.get("LLM_API_KEY") or None,
            llm_model=env.get("LLM_MODEL", DEFAULT_LLM_MODEL),
            embeddings_url=env.get("EMBEDDINGS_URL") or None,
            embeddings_api_key=env.get("EMBEDDINGS_API_KEY") or None,
            embeddings_model=env.get("EMBEDDINGS_MODEL") or None,
            team_roles=team_roles,
        )

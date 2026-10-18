"""Router settings, read once from the environment (see router/.env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

from contracts import Topic

REQUIRED = (
    "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "VENDOR_ADDRESS", "ROUTER_SEED",
    "CHANNEL_RD", "CHANNEL_QA_RA", "CHANNEL_ENGINEERING", "CHANNEL_SALES_OPS",
    "FLAGGED_FALLBACK_CHANNEL", "FLAGGED_FALLBACK_USER",
)


@dataclass(frozen=True)
class Config:
    slack_bot_token: str
    slack_app_token: str
    vendor_address: str
    router_seed: str
    router_address: str                 # informational; the agent derives its address from the seed
    channel_rd: str
    channel_qa_ra: str
    channel_engineering: str
    channel_sales_ops: str
    flagged_fallback_channel: str
    flagged_fallback_user: str          # a named reviewer's Slack user ID (D23)
    vendor_wait_s: float = 30.0         # A8 / T10
    reminder_after_s: float = 900.0     # D8
    store_path: str = "router/router.db"
    router_port: int = 8002

    def topic_channel(self, topic: Topic) -> str:
        return {
            Topic.RND: self.channel_rd,
            Topic.QA_RA: self.channel_qa_ra,
            Topic.ENGINEERING: self.channel_engineering,
            Topic.SALES_OPS: self.channel_sales_ops,
        }[topic]

    def expert_channels(self) -> frozenset[str]:
        return frozenset({self.channel_rd, self.channel_qa_ra, self.channel_engineering,
                          self.channel_sales_ops, self.flagged_fallback_channel})

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Config":
        env = os.environ if env is None else env
        missing = [k for k in REQUIRED if not env.get(k)]
        if missing:
            raise SystemExit(f"missing env vars: {', '.join(missing)} (see router/.env.example)")
        return cls(
            slack_bot_token=env["SLACK_BOT_TOKEN"],
            slack_app_token=env["SLACK_APP_TOKEN"],
            vendor_address=env["VENDOR_ADDRESS"],
            router_seed=env["ROUTER_SEED"],
            router_address=env.get("ROUTER_ADDRESS", ""),
            channel_rd=env["CHANNEL_RD"],
            channel_qa_ra=env["CHANNEL_QA_RA"],
            channel_engineering=env["CHANNEL_ENGINEERING"],
            channel_sales_ops=env["CHANNEL_SALES_OPS"],
            flagged_fallback_channel=env["FLAGGED_FALLBACK_CHANNEL"],
            flagged_fallback_user=env["FLAGGED_FALLBACK_USER"],
            vendor_wait_s=float(env.get("VENDOR_WAIT_S", 30)),
            reminder_after_s=float(env.get("REMINDER_AFTER_S", 900)),
            store_path=env.get("ROUTER_STORE_PATH", "router/router.db"),
            router_port=int(env.get("ROUTER_PORT", 8002)),
        )

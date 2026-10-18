"""Hosted Agentverse entry point for the Compliance Guard (becomes agent.py).

Not importable from the repo: hosted Agentverse predefines `agent`, and the
imports refer to the flat files `python -m guard.hosted.build` writes next to
it. Secrets arrive as env vars: ALLOWED_SENDERS, LLM_API_KEY, LLM_MODEL (gemini-* or
Anthropic), LLM_WORKSPACE_ID (Anthropic only).
"""
import os

from uagents import Context

from contracts import GuardRequest
from guard_agent import build_core, handle_request, parse_allowed_senders
from guard_policy import POLICY

CORE = build_core(policy=POLICY)
ALLOWED = parse_allowed_senders(os.environ.get("ALLOWED_SENDERS"))


@agent.on_event("startup")  # noqa: F821 (predefined by hosted Agentverse)
async def report_config(ctx: Context):
    judge = "on" if CORE.judge is not None else "OFF (LLM_API_KEY not set)"
    ctx.logger.info(f"GUARD ready: address={ctx.agent.address} judge={judge} allowed_senders={len(ALLOWED)}")
    if not ALLOWED:
        ctx.logger.warning("GUARD ALLOWED_SENDERS is empty: every GuardRequest will be dropped")


@agent.on_message(model=GuardRequest)  # noqa: F821
async def on_guard_request(ctx: Context, sender: str, msg: GuardRequest):
    await handle_request(ctx, sender, msg, CORE, ALLOWED)

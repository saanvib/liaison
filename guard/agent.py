"""Compliance Guard as a uAgent: wiring only, all logic lives in guard.core.

    python -m guard.agent          # needs GUARD_SEED and ALLOWED_SENDERS (see guard/.env.example)

Accepts GuardRequest only from ALLOWED_SENDERS (the Vendor and Router
addresses, D14); anything else is logged and dropped. An empty allowlist drops
everything. GuardCore.check runs in a worker thread so the LLM calls don't
block the event loop. Without LLM_API_KEY the judge is unavailable, so drafts
that pass the rules come back needs_review (fail closed).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from uagents import Agent, Context

from contracts import GuardRequest, GuardVerdict

from guard.core import GuardCore
from guard.grounding import Grounding
from guard.judge import Judge

log = logging.getLogger(__name__)

DEFAULT_PORT = 8001


def parse_allowed_senders(value: Optional[str]) -> frozenset[str]:
    return frozenset(a.strip() for a in (value or "").split(",") if a.strip())


def build_core(policy: Optional[dict] = None) -> GuardCore:
    """`policy=None` reads guard/policy.json; the hosted build passes the embedded copy (no file reads there)."""
    if not os.environ.get("LLM_API_KEY"):
        log.warning("LLM_API_KEY not set: judge unavailable, drafts that pass the rules will be needs_review")
        return GuardCore(judge=None, grounding=Grounding(), policy=policy)
    from guard.llm import AnthropicLLM

    llm = AnthropicLLM()
    return GuardCore(judge=Judge(llm), grounding=Grounding(llm), policy=policy)


async def handle_request(
    ctx: Context, sender: str, msg: GuardRequest, core: GuardCore, allowed: frozenset[str]
) -> Optional[GuardVerdict]:
    """Check and reply if the sender is allowed; returns the verdict sent, or None if dropped."""
    if sender not in allowed:
        ctx.logger.warning(f"dropped GuardRequest {msg.request_id} from unlisted sender {sender}")
        return None
    verdict = await asyncio.to_thread(core.check, msg)
    ctx.logger.info(f"{msg.request_id}: {verdict.verdict.value}/{verdict.category.value}")
    await ctx.send(sender, verdict)
    return verdict


def create_agent(
    seed: Optional[str] = None,
    allowed: Optional[frozenset[str]] = None,
    core: Optional[GuardCore] = None,
    port: Optional[int] = None,
    endpoint: Optional[list[str]] = None,
) -> Agent:
    seed = seed or os.environ.get("GUARD_SEED")
    if not seed:
        raise SystemExit("GUARD_SEED is not set (see guard/.env.example)")
    allowed = allowed if allowed is not None else parse_allowed_senders(os.environ.get("ALLOWED_SENDERS"))
    core = core or build_core()
    port = port or int(os.environ.get("GUARD_PORT", DEFAULT_PORT))

    agent = Agent(
        name="liaison-guard",
        seed=seed,
        port=port,
        endpoint=endpoint if endpoint is not None else [f"http://127.0.0.1:{port}/submit"],
        description="Liaison Compliance Guard: checks questions, drafts and expert answers.",
    )
    if not allowed:
        log.warning("ALLOWED_SENDERS is empty: every GuardRequest will be dropped")

    # No `replies=` declaration: uAgents would then log an ERROR for every intentionally dropped request.
    @agent.on_message(model=GuardRequest)
    async def on_guard_request(ctx: Context, sender: str, msg: GuardRequest):
        await handle_request(ctx, sender, msg, core, allowed)

    return agent


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    guard = create_agent()
    print(f"Guard address: {guard.address}")
    guard.run()

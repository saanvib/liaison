"""Hosted smoke client (becomes agent.py of a second, throwaway hosted agent).

On startup it sends one GuardRequest per mode to GUARD_ADDRESS and logs each
GuardVerdict as a SMOKE line. Its own address must be in the Guard's
ALLOWED_SENDERS, or the Guard drops the requests (which also proves D14).
"""
import os

from uagents import Context

from contracts import GuardVerdict
from guard_smoke_requests import REQUESTS

GUARD_ADDRESS = os.environ.get("GUARD_ADDRESS", "").strip()


@agent.on_event("startup")  # noqa: F821 (predefined by hosted Agentverse)
async def send_requests(ctx: Context):
    ctx.logger.info(f"SMOKE client address (add to the Guard's ALLOWED_SENDERS): {ctx.agent.address}")
    if not GUARD_ADDRESS:
        ctx.logger.error("SMOKE GUARD_ADDRESS secret is not set")
        return
    for req in REQUESTS:
        await ctx.send(GUARD_ADDRESS, req)
        ctx.logger.info(f"SMOKE sent {req.request_id}")


@agent.on_message(model=GuardVerdict)  # noqa: F821
async def on_verdict(ctx: Context, sender: str, msg: GuardVerdict):
    ctx.logger.info(f"SMOKE {msg.request_id}: {msg.verdict.value}/{msg.category.value}: {msg.reason}")

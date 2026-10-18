"""Local smoke run: the Guard agent answers a hand-built GuardRequest in each mode.

    python -m guard.smoke

Runs the Guard and two test clients (one allowlisted, one not) in a uAgents
Bureau, so messages are delivered in-process. Exits 0 when the allowlisted
client gets a verdict for screen, draft and egress and the unlisted one gets
nothing; 1 otherwise. Uses fixed dev seeds; never use them for a real agent.
"""
from __future__ import annotations

import asyncio
import sys

from uagents import Agent, Bureau, Context

from contracts import GuardRequest, GuardVerdict, Source

from guard.agent import create_agent
from guard.smoke_requests import REQUESTS

# uAgents updates the agent's status on Agentverse before running startup handlers;
# that call has taken ~33 s, so the clients' sends start late. Message handling isn't delayed.
TIMEOUT_S = 90.0


async def main() -> int:
    # report_events=False: otherwise startup handlers wait (~35 s) on an Agentverse telemetry lookup.
    quiet = dict(report_events=False, mark_inactive_on_shutdown=False)
    client = Agent(name="guard-smoke-client", seed="liaison-dev-only guard smoke client", **quiet)
    stranger = Agent(name="guard-smoke-stranger", seed="liaison-dev-only guard smoke stranger", **quiet)
    guard = create_agent(
        seed="liaison-dev-only guard smoke guard",
        allowed=frozenset({client.address}),
        port=8101,
        endpoint=[],
    )
    received: dict[str, GuardVerdict] = {}
    stranger_got: list[GuardVerdict] = []
    done = asyncio.Event()

    @client.on_event("startup")
    async def send_all(ctx: Context):
        for req in REQUESTS:
            status = await ctx.send(guard.address, req)
            ctx.logger.info(f"sent {req.request_id}: {status}")

    @client.on_message(model=GuardVerdict)
    async def on_verdict(ctx: Context, sender: str, msg: GuardVerdict):
        received[msg.request_id] = msg
        if len(received) == len(REQUESTS):
            done.set()

    stranger_sent: list[bool] = []

    @stranger.on_event("startup")
    async def send_forbidden(ctx: Context):
        status = await ctx.send(guard.address, GuardRequest(
            request_id="smoke-stranger", source=Source.VENDOR_AGENT, question="How do I store the kit?", draft_answer=""))
        stranger_sent.append(status.status.value == "delivered")

    @stranger.on_message(model=GuardVerdict)
    async def on_stranger_verdict(ctx: Context, sender: str, msg: GuardVerdict):
        stranger_got.append(msg)

    bureau = Bureau(agents=[guard, client, stranger], port=8100, endpoint=[])
    task = asyncio.create_task(bureau.run_async())
    async def finished():
        await done.wait()
        while not stranger_sent:
            await asyncio.sleep(0.2)
        await asyncio.sleep(2.0)  # give a wrongly-answered stranger time to show up

    try:
        await asyncio.wait_for(finished(), TIMEOUT_S)
    except asyncio.TimeoutError:
        pass
    finally:
        task.cancel()

    print("\nGuard smoke run:")
    for req in REQUESTS:
        v = received.get(req.request_id)
        print(f"  {req.request_id:<14} " + (f"{v.verdict.value}/{v.category.value}: {v.reason}" if v else "NO REPLY"))
    print(f"  unlisted sender: request delivered={bool(stranger_sent and stranger_sent[0])}, "
          f"got {len(stranger_got)} replies (expected 0)")
    ok = len(received) == len(REQUESTS) and stranger_sent == [True] and not stranger_got
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

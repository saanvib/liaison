"""Hosted Agentverse entry point for the Vendor Agent (becomes agent.py).

Not importable from the repo: hosted Agentverse predefines `agent`, and the
imports refer to the flat files `python -m vendor_agent.hosted.build` writes
next to it. Secrets arrive as env vars: ROUTER_ADDRESS, GUARD_ADDRESS,
INGEST_ADDRESS, APPROVER_ALLOWLIST, LLM_API_KEY, LLM_MODEL. VENDOR_SEED is
unused hosted -- Agentverse assigns the address itself.
"""
from uagents import Context

from vendor_agent_agent import wire
from vendor_agent_config import Config

CONFIG = Config.from_env()
CORE = wire(agent, CONFIG, storage=agent.storage)  # noqa: F821 (predefined by hosted Agentverse)


@agent.on_event("startup")  # noqa: F821
async def report_config(ctx: Context):
    ctx.logger.info(
        f"VENDOR ready: address={ctx.agent.address} "
        f"router={CONFIG.router_address} guard={CONFIG.guard_address} "
        f"llm_configured={bool(CONFIG.llm_api_key)}"
    )

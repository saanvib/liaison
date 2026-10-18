"""Router as a uAgent (local mailbox agent): wiring only, all logic lives in router.core.

    python -m router.agent          # needs the env in router/.env.example

- Chat Protocol: ChatMessage replies from the Vendor Agent are acknowledged and fed to
  the VendorClient (only VENDOR_ADDRESS is trusted, D14).
- Escalation messages go to RouterCore.on_vendor_escalation.
- The Slack Socket Mode handler runs in a background thread; Slack handlers schedule
  core coroutines on the agent's event loop with asyncio.run_coroutine_threadsafe.
- Every 30 s the reminder tick runs (D8).
- The real expert matcher (experts.match, Person 2) is plugged in here and nowhere else.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Coroutine, Optional

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec,
)

from contracts import Escalation

from router.config import Config
from router.core import RouterCore
from router.store import Store
from router.vendor_client import VendorClient

log = logging.getLogger(__name__)

TICK_S = 30.0


def person_lookup(directory) -> callable:
    by_slack = {e.slack_user_id: (e.display_name, e.role) for e in directory}
    return by_slack.get


def create_agent(config: Optional[Config] = None, core: Optional[RouterCore] = None,
                 start_slack: bool = True) -> tuple[Agent, RouterCore]:
    config = config or Config.from_env()
    agent = Agent(
        name="liaison-router",
        seed=config.router_seed,
        port=config.router_port,
        mailbox=True,
        description="Liaison Router: the Slack bot that asks the Vendor Agent and routes escalations to experts.",
    )
    if config.router_address and config.router_address != agent.address:
        log.warning("ROUTER_ADDRESS (%s) does not match this seed's address (%s)", config.router_address, agent.address)

    runtime: dict = {"ctx": None, "loop": None}

    async def send_text(address: str, text: str) -> None:
        await runtime["ctx"].send(address, ChatMessage(content=[TextContent(text=text)]))

    async def send_model(address: str, model) -> None:
        await runtime["ctx"].send(address, model)

    vendor = VendorClient(config.vendor_address, send_text, send_model, config.vendor_wait_s)
    if core is None:
        import experts
        from slack_sdk import WebClient

        from router.slack_app import SlackWebPort

        core = RouterCore(
            experts.match, vendor, SlackWebPort(WebClient(token=config.slack_bot_token)),
            Store(config.store_path), config, time.time,
            allowlist=experts.allowlist, lookup_person=person_lookup(experts.load_directory()),
        )

    chat_proto = Protocol(spec=chat_protocol_spec)

    @chat_proto.on_message(ChatMessage)
    async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
        await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
        vendor.on_chat_reply(sender, msg.text())

    @chat_proto.on_message(ChatAcknowledgement)
    async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
        ctx.logger.debug(f"ack from {sender} for {msg.acknowledged_msg_id}")

    agent.include(chat_proto, publish_manifest=True)

    @agent.on_message(model=Escalation)
    async def on_escalation(ctx: Context, sender: str, msg: Escalation):
        await core.on_vendor_escalation(sender, msg)

    @agent.on_event("startup")
    async def on_startup(ctx: Context):
        runtime["ctx"], runtime["loop"] = ctx, asyncio.get_running_loop()
        if start_slack:
            _start_slack(config, core, runtime["loop"])

    @agent.on_interval(period=TICK_S)
    async def on_tick(ctx: Context):
        await core.tick()

    return agent, core


def _start_slack(config: Config, core: RouterCore, loop: asyncio.AbstractEventLoop) -> None:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    from router.slack_app import register

    def report(fut) -> None:
        if not fut.cancelled() and fut.exception():
            log.error("slack task failed", exc_info=fut.exception())

    def dispatch(coro: Coroutine) -> None:
        asyncio.run_coroutine_threadsafe(coro, loop).add_done_callback(report)

    app = App(token=config.slack_bot_token)
    register(app, core, dispatch)
    handler = SocketModeHandler(app, config.slack_app_token)
    threading.Thread(target=handler.start, name="slack-socket-mode", daemon=True).start()
    log.info("Slack Socket Mode started")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    router, _ = create_agent()
    print(f"Router address: {router.address}")
    router.run()

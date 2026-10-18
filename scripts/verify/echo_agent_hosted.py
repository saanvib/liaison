"""Verification T9 (b): a hosted Agentverse echo agent for the Chat Protocol round trip.

Paste this whole file into a new hosted agent on agentverse.ai (Agents > + New Agent > Blank Agent),
replacing agent.py, then Start it. Hosted agents get a predefined `agent`, so none is created here.

Point the local Router at it (VENDOR_ADDRESS=<this agent's address> in router/.env, restart the Router)
and run `/ask hello` in Slack. The Router sends the A2 envelope; this agent replies with the A2 reply
envelope, so Slack should show "Echo from hosted agent: hello" in the question's thread.

It also counts messages in agent storage (ctx.storage), which gives a first look at check (c):
restart the agent and see whether the count continues.
"""
from datetime import datetime, timezone
from uuid import uuid4

from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)

chat_proto = Protocol(spec=chat_protocol_spec)


@chat_proto.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = "".join(c.text for c in msg.content if isinstance(c, TextContent))
    count = int(ctx.storage.get("count") or 0) + 1
    ctx.storage.set("count", count)
    ctx.logger.info(f"message #{count} from {sender}: {text[:80]!r}")

    head, sep, question = text.partition("\n\n")
    if sep and head.startswith("LIAISON-REF: "):  # from the Router: answer in the A2 reply envelope
        reply = f"{head}\nOUTCOME: answered\n\nEcho from hosted agent: {question} (message #{count})"
    else:
        reply = f"Echo from hosted agent: {text} (message #{count})"
    await ctx.send(sender, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                       content=[TextContent(type="text", text=reply)]))


@chat_proto.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


agent.include(chat_proto, publish_manifest=True)  # noqa: F821  (`agent` is provided by Agentverse)

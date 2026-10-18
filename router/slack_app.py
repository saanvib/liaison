"""Slack bot (Bolt, Socket Mode): handlers only, all logic lives in router.core.

Every handler calls ack() as its very first statement (Slack's 3 s limit), then hands
the work to `dispatch`, which schedules the core coroutine on the agent's event loop.

Entry points for a question: `/ask <question>` in a channel (the bot posts the question
and answers in that post's thread), a DM to the bot, or an @mention. Experts get escalations
by DM and answer in that DM's thread (or in EXPERTS_CHANNEL threads for a no-match).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Callable, Coroutine, Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.webhook import WebhookClient

from router.core import RouterCore, SlackError

log = logging.getLogger(__name__)

Dispatch = Callable[[Coroutine], None]
ACTIONS = ("approve_ask", "cancel_ask", "approve_draft", "edit_draft")
_MENTION = re.compile(r"<@[A-Z0-9]+>")


def handlers(core: RouterCore, dispatch: Dispatch) -> dict[str, Callable]:
    """The Bolt listener functions, by name; register() attaches them. Plain functions so tests can call them."""

    def on_ask(ack, command):
        ack()
        dispatch(core.on_ask_command(command["user_id"], command["channel_id"], command.get("text", ""),
                                     command.get("response_url", "")))

    def on_app_mention(ack, event):
        ack()
        text = _MENTION.sub("", event.get("text", "")).strip()
        if text:
            dispatch(core.on_mention(event["user"], event["channel"], event.get("thread_ts") or event["ts"], text))

    def on_message(ack, event):
        ack()
        if event.get("bot_id") or event.get("subtype"):  # bot posts, edits (message_changed), deletes
            return
        user, channel, ts, text = event.get("user"), event["channel"], event["ts"], event.get("text", "")
        thread_ts = event.get("thread_ts")
        if not user:
            return
        if event.get("channel_type") == "im":  # a question, or an expert's reply in a thread the bot DMed
            dispatch(core.on_direct_message(user, channel, ts, thread_ts, text))
        elif thread_ts and thread_ts != ts:  # a thread reply; the core ignores non-escalation threads
            dispatch(core.on_thread_reply(channel, thread_ts, user, text))

    def on_action(ack, body, action):
        ack()
        esc_id, user, channel = action["value"], body["user"]["id"], body["channel"]["id"]
        name = action["action_id"]
        if name == "approve_ask":
            dispatch(core.on_approve_ask(esc_id, user, channel))
        elif name == "cancel_ask":
            dispatch(core.on_cancel_ask(esc_id, user, channel))
        elif name == "approve_draft":
            dispatch(core.on_approve_draft(esc_id, user, channel))
        elif name == "edit_draft":
            dispatch(core.on_edit_draft(esc_id, user, channel, body["trigger_id"]))

    def on_edit_submit(ack, body, view):
        ack()
        text = view["state"]["values"]["answer"]["answer_text"]["value"] or ""
        dispatch(core.on_edit_submit(view["private_metadata"], body["user"]["id"], text))

    return {"ask": on_ask, "app_mention": on_app_mention, "message": on_message,
            "action": on_action, "edit_submit": on_edit_submit}


def register(app, core: RouterCore, dispatch: Dispatch) -> None:
    h = handlers(core, dispatch)
    app.command("/ask")(h["ask"])
    app.event("app_mention")(h["app_mention"])
    app.event("message")(h["message"])
    for name in ACTIONS:
        app.action(name)(h["action"])
    app.view("edit_draft_modal")(h["edit_submit"])


class SlackWebPort:
    """router.core.SlackPort over slack_sdk's WebClient; blocking calls run in a worker thread."""

    def __init__(self, client: WebClient):
        self.client = client

    async def _call(self, method: str, **kwargs) -> dict:
        try:
            return await asyncio.to_thread(getattr(self.client, method), **kwargs)
        except SlackApiError as e:
            raise SlackError(e.response.get("error", "unknown_error")) from e

    async def post(self, channel: str, text: str, thread_ts: Optional[str] = None,
                   blocks: Optional[list] = None) -> str:
        resp = await self._call("chat_postMessage", channel=channel, text=text, thread_ts=thread_ts, blocks=blocks)
        return resp["ts"]

    async def update(self, channel: str, ts: str, text: str, blocks: Optional[list] = None) -> None:
        await self._call("chat_update", channel=channel, ts=ts, text=text, blocks=blocks)

    async def ephemeral(self, channel: str, user: str, text: str, thread_ts: Optional[str] = None) -> None:
        await self._call("chat_postEphemeral", channel=channel, user=user, text=text, thread_ts=thread_ts)

    async def respond(self, response_url: str, text: str) -> None:
        if response_url:
            await asyncio.to_thread(WebhookClient(response_url).send, text=text, response_type="ephemeral")

    async def open_modal(self, trigger_id: str, view: dict) -> None:
        await self._call("views_open", trigger_id=trigger_id, view=view)

    async def open_dm(self, user_id: str) -> str:
        return (await self._call("conversations_open", users=user_id))["channel"]["id"]

    async def user_name(self, user_id: str) -> str:
        user = (await self._call("users_info", user=user_id))["user"]
        return user.get("profile", {}).get("display_name") or user.get("real_name") or user_id

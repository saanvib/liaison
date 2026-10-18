"""Slack wording and Block Kit payloads for the Router. No logic."""
from __future__ import annotations

from typing import Optional

VENDOR_UNREACHABLE = "I couldn't reach the knowledge base. Please try again."
ASK_USAGE = "Usage: `/ask <your question>`"
NOT_IN_CHANNEL = "I can't post here yet. Invite me first with `/invite @Liaison`, then try `/ask` again."
ONLY_ASKER = "Only the person who asked can approve or cancel this."
NOT_ALLOWED = "Only the matched expert or this topic's listed experts can answer this escalation."
ALREADY_DONE = "This escalation has already been handled."
CANCELLED = "Cancelled. Nothing was sent to an expert."
EXPERT_DELIVERY_FAILED = "I couldn't deliver the question to the expert. Nothing was sent; you can click Approve again."
FLAGGED_BANNER = ":warning: *Flagged: potentially classified information.* The automatic check could not screen this question. Handle it with care; the answer will not be stored."
EXPERT_INSTRUCTIONS = "Reply in this thread. The first reply is sent to the asker as your answer."

SECTION_LIMIT = 2900  # Slack section text limit is 3000 characters


def clip(text: str) -> str:
    return text if len(text) <= SECTION_LIMIT else text[: SECTION_LIMIT - 1] + "…"


def quote(text: str) -> str:
    return "\n".join("> " + line for line in text.splitlines() or [""])


def asked_via_command(user_id: str, question: str) -> str:
    return f"<@{user_id}> asked:\n{quote(question)}"


def recipient_label(user_id: Optional[str], name: Optional[str], role: Optional[str], channel_id: str,
                    direct: bool = True) -> str:
    """Who gets the question: a person by DM, a person mentioned in the experts channel, or the channel."""
    if user_id:
        who = (f"*{name}*" + (f", {role}" if role else "") + f" (<@{user_id}>)") if name else f"<@{user_id}>"
        return f"{who} by direct message" if direct else f"{who} in <#{channel_id}>"
    return f"the experts channel <#{channel_id}>"


def proposal_blocks(escalation_id: str, question: str, recipient: str, flagged: bool) -> tuple[str, list]:
    if flagged:
        intro = ("This question may contain *potentially classified information*, so I won't route it "
                 f"automatically. I can send it to the fixed reviewer: {recipient}.")
    else:
        intro = f"I couldn't answer this from the documents. I propose asking {recipient}."
    text = f"{intro}\n\nThis exact question would be sent:\n{quote(clip(question))}\n\nNothing is sent until you approve."
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "elements": [
            {"type": "button", "action_id": "approve_ask", "style": "primary", "value": escalation_id,
             "text": {"type": "plain_text", "text": "Approve and send"}},
            {"type": "button", "action_id": "cancel_ask", "value": escalation_id,
             "text": {"type": "plain_text", "text": "Cancel"}},
        ]},
    ]
    return text, blocks


def proposal_resolved(text: str) -> list:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def expert_post(escalation_id: str, question: str, mention: Optional[str], flagged: bool,
                draft: Optional[str], guard_reason: Optional[str]) -> tuple[str, list]:
    parts = []
    if flagged:
        parts.append(FLAGGED_BANNER)
    parts.append((f"<@{mention}> " if mention else "") + "a colleague needs an expert answer:")
    parts.append(quote(clip(question)))
    text = "\n\n".join(parts)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if draft:
        note = f"Draft from the documents (not sent: {guard_reason})" if guard_reason else "Draft from the documents (not sent)"
        blocks += [
            {"type": "section", "text": {"type": "mrkdwn", "text": clip(f"*{note}*\n{quote(draft)}")}},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": "approve_draft", "style": "primary", "value": escalation_id,
                 "text": {"type": "plain_text", "text": "Approve as is"}},
                {"type": "button", "action_id": "edit_draft", "value": escalation_id,
                 "text": {"type": "plain_text", "text": "Edit"}},
            ]},
        ]
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": EXPERT_INSTRUCTIONS}]})
    return text, blocks


def edit_modal(escalation_id: str, draft: str) -> dict:
    return {
        "type": "modal",
        "callback_id": "edit_draft_modal",
        "private_metadata": escalation_id,
        "title": {"type": "plain_text", "text": "Edit answer"},
        "submit": {"type": "plain_text", "text": "Send answer"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [{
            "type": "input", "block_id": "answer",
            "label": {"type": "plain_text", "text": "Final answer (sent as typed)"},
            "element": {"type": "plain_text_input", "action_id": "answer_text", "multiline": True,
                        "initial_value": draft[:3000]},
        }],
    }


def answer_for_asker(name: str, role: Optional[str], answer: str) -> str:
    who = f"{name}, {role}" if role else name
    return f"*Answer from {who}:*\n\n{answer}"


def expert_thanks(flagged: bool) -> str:
    return "Thanks, your answer was sent to the asker." + (" It was not stored (flagged)." if flagged else " It will be reused for the same question.")


def reminder(mention: Optional[str]) -> str:
    return (f"<@{mention}> " if mention else "") + "Reminder: this question is still waiting for an answer."

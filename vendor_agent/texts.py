"""Fixed reply texts (D4, D32)."""
from contracts import Topic

RETRY_TEXT = (
    "I wasn't able to verify that request. Could you try asking again, "
    "maybe with a different phrasing?"
)

REFUSAL_TEXT = (
    "I can't process questions that may contain patient information. "
    "Please remove any patient details and ask again."
)

HOLDING_TEXT = "I've routed your question to the appropriate team. No reply is needed from you right now."


def team_role_reply(topic: Topic, config) -> str:
    team = config.team_roles[topic]
    return f"I couldn't find an answer, so I've noted this for {team}. Nothing was sent on your behalf."

"""Public expert-matching API (00-shared-interfaces.md). Used by the Router's agent wiring only."""
from typing import Optional

from contracts import Topic

from experts.matcher import Expert, MatchResult, default_matcher
from experts.matcher import load_directory as _load_directory

__all__ = ["Expert", "MatchResult", "match", "load_directory", "allowlist"]


def match(question: str, topic: Optional[Topic] = None) -> MatchResult:
    """Best expert for the question (restricted to `topic` if given); fallback=True means use the topic channel."""
    return default_matcher().match(question, topic)


def load_directory() -> list[Expert]:
    return _load_directory()


def allowlist(topic: Topic) -> set[str]:
    """Slack user IDs allowed to reply or click in that topic's channel (D11)."""
    return {e.slack_user_id for e in _load_directory() if topic in e.topics}

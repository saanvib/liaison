"""Every Slack handler calls ack() before the core is touched (Slack's 3 s limit)."""
import pytest

from router.slack_app import ACTIONS, handlers, register


class SpyCore:
    """Records each core call; the event log shows whether ack came first."""

    def __init__(self, log):
        self.log = log

    def __getattr__(self, name):
        def call(*args):
            self.log.append(("core", name, args))
            return ("coro", name)
        return call


@pytest.fixture
def setup():
    log = []
    h = handlers(SpyCore(log), dispatch=lambda coro: log.append(("dispatch", coro)))
    ack = lambda *a, **k: log.append(("ack",))
    return h, ack, log


def assert_ack_first(log, core_method):
    assert log[0] == ("ack",), log
    assert [e[1] for e in log if e[0] == "core"] == [core_method]


def test_ask_command(setup):
    h, ack, log = setup
    h["ask"](ack=ack, command={"user_id": "U1", "channel_id": "C1", "text": "shelf life?", "response_url": "https://r"})
    assert_ack_first(log, "on_ask_command")
    assert log[1][2] == ("U1", "C1", "shelf life?", "https://r")


def test_app_mention_strips_mention(setup):
    h, ack, log = setup
    h["app_mention"](ack=ack, event={"user": "U1", "channel": "C1", "ts": "1.1", "text": "<@UBOT> shelf life?"})
    assert_ack_first(log, "on_mention")
    assert log[1][2] == ("U1", "C1", "1.1", "shelf life?")


def test_dm_is_a_question(setup):
    h, ack, log = setup
    h["message"](ack=ack, event={"user": "U1", "channel": "D1", "channel_type": "im", "ts": "1.1", "text": "hi?"})
    assert_ack_first(log, "on_mention")


def test_thread_reply_goes_to_core(setup):
    h, ack, log = setup
    h["message"](ack=ack, event={"user": "U2", "channel": "C_RD", "channel_type": "channel", "ts": "2.2",
                                 "thread_ts": "1.1", "text": "30 days"})
    assert_ack_first(log, "on_thread_reply")
    assert log[1][2] == ("C_RD", "1.1", "U2", "30 days")


@pytest.mark.parametrize("event", [
    {"bot_id": "B1", "user": "U2", "channel": "C", "ts": "2", "thread_ts": "1", "text": "x"},
    {"subtype": "message_changed", "channel": "C", "ts": "2", "thread_ts": "1"},
    {"user": "U2", "channel": "C", "channel_type": "channel", "ts": "1", "text": "top-level"},
])
def test_ignored_messages_still_ack(setup, event):
    h, ack, log = setup
    h["message"](ack=ack, event=event)
    assert log == [("ack",)]


@pytest.mark.parametrize("action_id,method", [
    ("approve_ask", "on_approve_ask"), ("cancel_ask", "on_cancel_ask"),
    ("approve_draft", "on_approve_draft"), ("edit_draft", "on_edit_draft"),
])
def test_actions(setup, action_id, method):
    h, ack, log = setup
    body = {"user": {"id": "U1"}, "channel": {"id": "C1"}, "trigger_id": "T1"}
    h["action"](ack=ack, body=body, action={"action_id": action_id, "value": "esc-1"})
    assert_ack_first(log, method)


def test_edit_modal_submit(setup):
    h, ack, log = setup
    view = {"private_metadata": "esc-1", "state": {"values": {"answer": {"answer_text": {"value": "typed"}}}}}
    h["edit_submit"](ack=ack, body={"user": {"id": "U1"}}, view=view)
    assert_ack_first(log, "on_edit_submit")
    assert log[1][2] == ("esc-1", "U1", "typed")


def test_register_wires_every_listener():
    from slack_bolt import App

    app = App(token="xoxb-test", signing_secret="x", token_verification_enabled=False)
    register(app, SpyCore([]), dispatch=lambda c: None)
    assert len(app._listeners) == 3 + len(ACTIONS) + 1

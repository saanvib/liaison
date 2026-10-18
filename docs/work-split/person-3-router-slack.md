# Person 3: Router + Slack bot + README + verification

**For the Claude session implementing this slice.** Read `docs/work-split/00-shared-interfaces.md` first. You own only these paths: `router/**`, `README.md`, `scripts/**`. Do not edit any other path. You may import `contracts` and, in `router/agent.py` only, the public `experts` API (Person 2); everywhere else take the matcher as an injected dependency so you can build with a fake. Estimated total: **~13 human hours**.

## Goal
The Router is the Slack bot, the only screen for internal employees. It takes a question from Slack, asks the Vendor Agent over the Chat Protocol, and shows the answer. When there is no answer it **names the matched expert and asks the asker's approval before anything is sent**, then posts the question to the expert, captures the first allowed reply, returns it to the asker, and sends an `ApprovedAnswer` to the Vendor Agent so the answer is learned. It also owns the root README and the pre-deployment verification.

## Build order, with estimates and "done when"

### Step 1 (~2 h): Slack app skeleton, config, store
- `router/config.py`: frozen dataclass from env: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `VENDOR_ADDRESS`, `ROUTER_SEED`, `ROUTER_ADDRESS`, channel IDs `CHANNEL_RD`, `CHANNEL_QA_RA`, `CHANNEL_ENGINEERING`, `CHANNEL_SALES_OPS`, `FLAGGED_FALLBACK_CHANNEL`, `FLAGGED_FALLBACK_USER` (a named reviewer's Slack ID), `VENDOR_WAIT_S` (default 30), `REMINDER_AFTER_S` (default 900).
- `router/store.py`: a small SQLite store (stdlib `sqlite3`) with one table of escalations: `escalation_id`, `asker_ref`, `question`, `topic`, `flagged`, `has_draft`, `draft_answer`, `state` (`proposed|approved|sent|answered|cancelled`), `proposed_expert_id`, `channel_id`, `thread_ts`, `created_at`, `sent_at`, `reminded`.
- `router/slack_app.py`: a Bolt app in Socket Mode (`slack_bolt.App`, `SocketModeHandler`). Handlers for: DMs and `app_mention` (a question), the actions `approve_ask`, `cancel_ask`, `approve_draft`, `edit_draft`, the modal submission `edit_draft_modal`, and thread replies in the expert channels. **Every handler calls `ack()` as its very first statement and then does the work off the request path** (Slack's 3 s limit).
- `router/SLACK_SETUP.md`: step-by-step creation of the Slack app: Socket Mode on, the scopes and events needed, the interactive-components setting, how to get the two tokens, which channels to create and invite the bot to.
- `router/.env.example` and `router/requirements.txt` (`slack-bolt`, `uagents`, `pyyaml` only if you need it).
- **Done when:** the app starts against a throwaway workspace and logs a received DM, or a Bolt dry-run test passes; a unit test proves each handler calls `ack` before touching the core.

### Step 2 (~2.5 h): `vendor_client.py`, the Chat Protocol link (A2, A8, D14)
- `VendorClient` sends `ChatMessage` text of the form `LIAISON-REF: <asker_ref>\n\n<question>` to `VENDOR_ADDRESS` and waits at most `VENDOR_WAIT_S` (30 s) for a reply. The reply is a `ChatMessage` whose text is `LIAISON-REF: <asker_ref>\nOUTCOME: <answered|refused|retry|holding|escalated>\n\n<body>`. Parse it with one function `parse_reply(text)` and build with one function `build_question(asker_ref, question)`; unit-test both.
- On `OUTCOME: escalated`, also wait (inside the same 30 s budget) for the typed `Escalation` message with the same `asker_ref`. Keep one `asyncio.Future` per `asker_ref`.
- `asker_ref` format is `slack:<channel_id>:<thread_ts>:<user_id>` (A3).
- **Timeout rule (T10, a critical gap from the review):** if the Vendor Agent doesn't answer in time, the asker gets a clear Slack message ("I couldn't reach the knowledge base. Please try again.") and the failure is logged. Never leave a Slack asker hanging.
- Accept replies and escalations **only from `VENDOR_ADDRESS`** (D14); log and drop anything else.
- **Done when:** tests with a fake transport cover: answered, refused, retry, holding, escalated followed by an `Escalation`, escalated with no `Escalation` (timeout message), a total timeout, and a message from the wrong sender being dropped.

### Step 3 (~3 h): `core.py`, escalation, proposal and approval (R16, D10, D22, D23)
`RouterCore(matcher, vendor, slack, store, config, clock)`; `matcher(question, topic) -> MatchResult` is injected (the real one is `experts.match`; write a fake with the same shape for tests). Methods (names are yours to refine, behavior is fixed):
- `on_slack_question(user_id, channel_id, thread_ts, text)`: build `asker_ref`, call `vendor.ask`, post the result in the asker's thread. `answered`, `refused`, `retry`, `holding` bodies are posted as they are.
- `on_escalation(esc: Escalation)`: store it. `flagged` is true exactly when `esc.guard_verdict is None and esc.draft_answer is None and esc.reason == EscalationReason.GUARD_NEEDS_REVIEW` (A5).
  - **Flagged (D23):** do **not** call the matcher and do not use any LLM. The recipient is the fixed fallback: the `FLAGGED_FALLBACK_USER` in `FLAGGED_FALLBACK_CHANNEL`. The proposal tells the asker the question may contain classified information and who the fixed recipient is.
  - **Not flagged:** `topic = esc.topic` (if `None`, let the matcher infer one); `result = matcher(esc.question, topic)`. The recipient is `result.expert` (mentioned in the topic's channel) or, when `result.fallback` is true, the topic channel alone.
- **Proposal (R16):** post in the asker's thread: that the question can't be answered from the documents, who you propose to ask (name and role, or the team channel), the exact question text that would be sent, and two buttons, **Approve and send** and **Cancel**. State becomes `proposed`. **Nothing is sent to an expert at this point.**
- **Clicks:** only the original asker may click; anyone else gets an ephemeral notice. Cancel sets `cancelled` and tells the asker nothing was sent. Approve sets `approved`, then posts in the recipient's channel (the question text, the flagged banner if flagged, the mention) and records `channel_id` and `thread_ts` of that post; state becomes `sent` and `sent_at` is stored.
- **Escalation that carries a draft (`esc.draft_answer` set, `reason` is `GUARD_NEEDS_REVIEW` or `GUARD_BLOCKED`):** after the asker approves, the expert post also shows the draft with buttons **Approve as is** and **Edit** (D17); Edit opens a modal and the typed text is the final answer, stored as typed with no re-check.
- **Done when:** `pytest router/tests/test_escalation.py` passes with fakes: a proposal names the matched expert; a no-match falls back to the topic channel; a flagged escalation never calls the matcher and goes to the fixed fallback; nothing is posted to an expert before approval; only the asker can click; cancel posts nothing to the expert; the draft case shows the two buttons.

### Step 4 (~2.5 h): expert replies, allowlist, reminder, `ApprovedAnswer` (D8, D11, D17, D14, D18)
- **Allowed repliers (D11):** the matched expert plus `experts.allowlist(topic)` (Slack user IDs). For a flagged escalation: `FLAGGED_FALLBACK_USER` plus the topic allowlist. Replies and clicks from anyone else are ignored with an ephemeral notice.
- **First reply wins (D8):** the first non-bot reply in the escalation thread from an allowed user is the answer. Mark the escalation `answered`; later replies and edits are ignored (no new action).
- **After an answer:** post it in the asker's thread, attributed ("Answer from <display name>, <role>"). **If not flagged**, send the Vendor Agent `ApprovedAnswer(escalation_id=..., question=..., answer=<text>, topic=<topic>, approved_by=f"{slack_user_id}|{display_name}", guard_verdict=None, citations=[], asker_ref=...)` (A4, D18). **If flagged, send no `ApprovedAnswer`** (D23: the answer is returned but never stored). Use the same path for **Approve as is** (answer is the draft) and **Edit** (answer is the typed text).
- **Reminder (D8):** `tick(now)` posts exactly one reminder in the thread once `REMINDER_AFTER_S` (15 min) has passed with no answer; set `reminded`; never reroute and never remind twice.
- **Done when:** `pytest router/tests/test_replies.py` passes with a fake clock: two replies gives the first; a disallowed user is ignored with an ephemeral notice; one reminder and only one; the `ApprovedAnswer` fields are exactly as above (check `approved_by` format and `guard_verdict is None`); a flagged escalation sends no `ApprovedAnswer`; the draft Approve and Edit paths work.

### Step 5 (~1 h): `router/agent.py`, uAgents wiring (no logic)
- `Agent(name="liaison-router", seed=ROUTER_SEED, mailbox=True)`. Chat Protocol handler for replies from the Vendor (`from uagents_core.contrib.protocols.chat import ChatMessage, ChatAcknowledgement, TextContent, chat_protocol_spec`; send a `ChatAcknowledgement` for each message). `@agent.on_message(Escalation)` hands the escalation to the core.
- Start the Slack Socket Mode handler in a background thread on startup and bridge into the agent's event loop with `asyncio.run_coroutine_threadsafe`. Run a periodic `tick` (every 30 s) from the agent's interval.
- Pass `experts.match` as the matcher here and nowhere else.
- **Done when:** `python -m router.agent` starts locally (with the fake env) and registers; a hand-sent `Escalation` reaches the core.

### Step 6 (~2 h): root README, scripts, and the pre-deployment verification (T9)
- `README.md` (repo root): what Liaison is in three sentences; the architecture diagram from the "Diagram (after amendments)" section of `docs/designs/liaison.md`; who owns what (link `docs/work-split/`); setup and env for each agent (link each agent's `.env.example`); how to run locally; how to seed documents; how to run `pytest` and the Guard eval; known limits (link `TODOS.md`).
- `scripts/run_all.sh`: starts the local pieces for a dev run and prints the reminders needed for the hosted agents. `scripts/seed_docs.sh`: `python -m vendor_agent.ingest vendor_agent/docs_seed --direct`.
- **Verification (T9), before anyone deploys.** Record the outcome of each check in a table in the README under "Verification": (a) the Agentverse hosted-agent package list and storage allow what the Vendor Agent and Guard need (an HTTP client, `json`, no local embedding library); (b) a `ChatMessage` round trip works between the local Router and a hosted agent; (c) a hosted KB write survives an agent restart. **If any check fails, stop and tell the team: it reopens decision D6 (hosting) and must not be fixed silently.**
- **Done when:** the README renders, both scripts run without error on a clean checkout, and the verification table is filled in with real results.

## Do not
- Do not edit `vendor_agent/`, `guard/`, `experts/`, `tests/`, `contracts/`, `docs/`.
- Do not send anything to an expert before the asker approves. Do not call the matcher or any LLM for a flagged question. Do not store a flagged answer.
- Do not add contract messages or fields. Do not implement async follow-up for ASI:One, Slack file-drop ingestion, or staleness.
- Do not touch the network in tests (fake Slack, fake vendor, fake clock).

## Definition of done for the whole slice
`pytest router` is green offline; the bot starts against a throwaway workspace and handles a question, an escalation, an approval, an expert reply and a reminder; `router/NOTES.md` lists env vars, the channel setup, and anything unverified.

# Router / Slack bot: notes

## Run
```
pip install -r contracts/requirements.txt -r experts/requirements.txt -r router/requirements.txt -r requirements-dev.txt
pytest router                                  # offline, no network
set -a; . router/.env; set +a; python -m router.agent
```
On the first start, open the "Agent inspector" link the agent prints and click **Connect** → **Mailbox**, so it can receive messages from hosted agents (until then the log shows `401 Could not validate credentials` for the mailbox).

If you're on the python.org build of Python and see `CERTIFICATE_VERIFY_FAILED`, run `export SSL_CERT_FILE=$(python -m certifi)` (or the "Install Certificates.command" that ships with it). `scripts/run_all.sh` does this for you.

## Environment variables (`router/.env.example`)
| Variable | Purpose |
|---|---|
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | Bot token (`xoxb-`) and app-level Socket Mode token (`xapp-`). See `SLACK_SETUP.md`. |
| `VENDOR_ADDRESS` | The Vendor Agent's address. Chat replies and `Escalation`s from anyone else are dropped (D14). |
| `ROUTER_SEED` | Seed for the Router's address. Give the printed address to the Vendor (`ROUTER_ADDRESS`) and Guard (`ALLOWED_SENDERS`). |
| `ROUTER_ADDRESS` | Optional; the Router warns if it doesn't match the seed. |
| `EXPERTS_CHANNEL` | Where an escalation goes when no expert matches, or the expert can't be DMed. |
| `FLAGGED_FALLBACK_USER` | The fixed reviewer who gets flagged questions by DM (D23). |
| `VENDOR_WAIT_S` | Wait for the Vendor Agent, default 30 (A8). |
| `REMINDER_AFTER_S` | One reminder to the expert, default 900 (D8). |
| `ROUTER_STORE_PATH`, `ROUTER_PORT` | SQLite file (default `router/router.db`) and local port (default 8002). |

## Channels
See `SLACK_SETUP.md`. There are two channels: one where everyone asks (for example #ask-liaison, with `/ask`), and #liaison-experts as the no-match fallback. Experts get escalations by DM. People can also ask by DM or with `@Liaison`.

## Flow (who sees what)
1. `/ask q` → the bot posts "<@asker> asked: q" and asks the Vendor Agent with `asker_ref = slack:<channel>:<that post's ts>:<user>`.
2. `answered | refused | retry | holding` → the body goes in that thread as is. If nothing arrives within 30 s: "I couldn't reach the knowledge base. Please try again."
3. `escalated` + `Escalation` → a proposal in the thread naming the matched expert (or the experts channel when nothing matches, or for a flagged question the fixed reviewer), the exact question, **Approve and send** / **Cancel**. Nothing is sent before Approve, and only the asker can click.
4. After Approve: a **DM to the expert** (question, flagged banner; for an escalation with a draft, the draft with **Approve as is** / **Edit**). The expert answers in that DM's thread. If the DM can't be opened (for example a placeholder ID), the post goes to `EXPERTS_CHANNEL` with an @mention instead, except a flagged question, which is never posted in a channel: the asker is told and can click Approve again.
5. The first allowed reply (or draft approval or edit) is the answer. It's posted to the asker as "Answer from <name>, <role>", and unless flagged, sent to the Vendor Agent as `ApprovedAnswer(approved_by="<slack id>|<name>", guard_verdict=None)`.
6. One reminder in the expert's thread (the DM, or the channel thread) after 15 min with no answer.

## Change from the split (team decision, 2026-10-17)
The split posts escalations "in the topic's channel". We changed that to **one ask channel + DM the matched expert** (no per-topic channels).
- **Replaced:** `CHANNEL_RD`, `CHANNEL_QA_RA`, `CHANNEL_ENGINEERING`, `CHANNEL_SALES_OPS` and `FLAGGED_FALLBACK_CHANNEL` are replaced by `EXPERTS_CHANNEL`, which is only used when no expert matches or the DM fails.
- **Unchanged:** asker approval first (R16), the matcher (D10), first reply wins and one reminder (D8), and the allowlist (D11). Only the DMed person can see a DM, so in practice the matched expert answers; in the fallback channel, the topic's listed experts can.
- **Flagged questions** are DMed to the reviewer, which keeps them out of shared channels (D23).
- **Slack** needs one more scope: `im:write`.

## Decisions I made where the split was silent (please confirm)
- **Who may answer a flagged escalation:** only `FLAGGED_FALLBACK_USER`. The split says "plus the topic allowlist", but a flagged escalation has `topic=None` (A5), so there is no topic allowlist to add.
- **Attribution when an allowlisted colleague answers instead of the matched expert:** their name and role from `experts/directory.yaml`. If they aren't in it, their Slack display name with the role "Team member". `approved_by` uses the same name.
- **Who may click Approve as is / Edit on a draft:** the same people who may reply.
- **`/ask` is the main entry point** (team request). DMs and `@Liaison` mentions work too, as the split lists. A top-level DM to the bot is always a question; an expert's answer must be a reply in the thread of the bot's DM.
- **An Escalation that arrives with no pending question** (for example after the 30 s timeout) still produces a proposal for the asker, so a hand-sent `Escalation` reaches the core (Step 5).

## Unverified / waiting on others
- **PR0 (Person 1):** until `ApprovedAnswer.guard_verdict` is Optional, building the `ApprovedAnswer` fails. The bot still posts the answer to the asker and logs the failure. Three tests are skipped with that reason. They passed when PR0 was applied locally.
- **The Vendor Agent's envelope (Person 1):** built to A2. Not yet tested against the real `vendor_agent/agent.py`.
- **`experts/directory.yaml`** now holds the 3 real member IDs (`APPROVER_ALLOWLIST=U0C9YESNKB7,U0C9YET9PM3,U0CA5L9TELR`). An ID not in the workspace makes the DM fail, and the escalation falls back to `EXPERTS_CHANNEL`.
- **Hosted checks (T9):** see the Verification table in the root README.
- uAgents 0.26.1 runs the `startup` handler (which starts Slack) only after its Agentverse status call. Person 2 measured that at about 33 s, so the bot can take up to about 30 s to come online after `python -m router.agent`.

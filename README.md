# Liaison

Liaison answers product questions for a regulated life-science company from its own documents, with citations, and routes the questions the documents can't answer to the right internal expert. A Compliance Guard checks questions and outgoing answers (patient data, clinical use of a research-use-only product, off-label use, comparative claims, pricing, roadmap). Expert answers are learned, so the next person who asks the same question gets an instant answer.

Employees use a Slack bot (`/ask`). External customers use ASI:One through the Chat Protocol. The three agents run on Fetch.ai uAgents and Agentverse.

## Architecture

```
 INTERNAL                                      EXTERNAL
 Slack (employees)                             ASI:One (customers)
   │ question                                    │ ChatMessage
   ▼                                             ▼
┌─ Router (local mailbox, Slack Socket Mode) ┐  ┌─ Vendor Agent (hosted) ─────────────────────────┐
│ allowlist D11 · first reply wins D8        │─▶│ 1 screen question via Guard (D7, D22)           │
│ expert match D10 · asker approval R16      │◀─│ 2 KB lookup; serve as stored (internal D20),    │
│ flagged question → fixed fallback D23      │  │   Guard-checked (external D33)                  │
└───────────────┬────────────────────────────┘  │ 3 retrieve + draft + Guard (10 s, D4)           │
                │ asker approves named expert   │ 4 not found: Slack proposes expert,             │
                ▼                               │   ASI:One names team or role (D32)              │
            expert (Slack)                      └───────────────┬─────────────────────────────────┘
                │ reply                                          │ ApprovedAnswer (verdict None, D18)
                └──────────── Router ──────────────────────────▶ │ accept iff trusted sender + pending id (D14)
                                                                 ▼
                                          Guard (hosted): rules → judge · eval bar (D13) · draft + external answers only
```

The D and R numbers point to the decision ledger in [`docs/designs/liaison.md`](docs/designs/liaison.md).

## Who owns what

| Path | Owner | Notes |
|---|---|---|
| `contracts/`, `vendor_agent/` | Person 1 | the shared messages, the Vendor Agent and KB |
| `guard/`, `experts/`, `tests/`, `pytest.ini` | Person 2 | the Compliance Guard and eval, the expert matcher, the loop test |
| `router/`, `README.md`, `scripts/` | Person 3 | the Slack bot, this README, the dev scripts, the verification |

Details: [`docs/work-split/`](docs/work-split/) (start with `00-shared-interfaces.md`).

## Setup

Python 3.12 is recommended (uAgents 0.26.1).

```
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r contracts/requirements.txt -r requirements-dev.txt \
            -r guard/requirements.txt -r experts/requirements.txt -r router/requirements.txt
pip install -r vendor_agent/requirements.txt   # once Person 1's slice is merged
```

Each agent reads its own env file. Copy the example to `.env` in the same folder and fill it in:

| Agent | Env example | Notes |
|---|---|---|
| Vendor Agent | `vendor_agent/.env.example` | `ROUTER_ADDRESS`, `GUARD_ADDRESS`, `APPROVER_ALLOWLIST`, LLM and embeddings keys |
| Guard | [`guard/.env.example`](guard/.env.example) | `ALLOWED_SENDERS` = the Vendor and Router addresses; see [`guard/NOTES.md`](guard/NOTES.md) |
| Router (Slack bot) | [`router/.env.example`](router/.env.example) | Slack tokens and channels; see [`router/SLACK_SETUP.md`](router/SLACK_SETUP.md) |

The addresses have to line up. Each agent prints its `agent1...` address on start.
- The Router's `VENDOR_ADDRESS` is the Vendor Agent's address.
- The Vendor Agent's `ROUTER_ADDRESS` is the Router's address.
- The Guard's `ALLOWED_SENDERS` lists both of them.

The Vendor Agent's approver list must match the expert directory (A12). Generate it with:

```
$ python -m experts.print_allowlist
APPROVER_ALLOWLIST=U_RND_1,U_RND_2,U_QARA_1,U_QARA_2,U_ENG_1,U_ENG_2,U_SALES_1,U_SALES_2
```

These are placeholder IDs. For a live Slack demo, swap in real member IDs ([`router/SLACK_SETUP.md`](router/SLACK_SETUP.md), step 4).

## Run locally

```
scripts/run_all.sh             # Guard, Vendor Agent and Router, each with its own .env
HOSTED=1 scripts/run_all.sh    # Router only, with the Vendor Agent and Guard hosted on Agentverse
```

Or start one agent at a time: `python -m guard.agent`, `python -m vendor_agent.agent`, `python -m router.agent`. In Slack, use `/ask <question>` in any channel the bot is in.

## Seed documents

```
scripts/seed_docs.sh           # python -m vendor_agent.ingest vendor_agent/docs_seed --direct
```

The documents are synthetic (a made-up research-use-only product). There is no real vendor data.

## Tests and the Guard eval

```
pytest                                          # everything, offline (no test touches the network)
pytest router                                   # one slice
python -m guard.eval.run_eval                   # offline eval; exits non-zero unless the bar is met
LLM_API_KEY=... python -m guard.eval.run_eval --llm real   # with the real LLM judge
```

The eval bar: 100% recall for every blocker category and at least 80% overall precision on the labeled set (D13).

## Verification (T9: before anyone deploys)

If any check fails, **stop and tell the team.** A failure reopens decision D6 (hosting) and must not be fixed quietly.

| Check | Result | Date | Notes |
|---|---|---|---|
| (a) Agentverse hosted package list and storage allow what the Vendor Agent and Guard need (an HTTP client, `json`, no local embedding library) | **not run yet** | | Person 2's desk check from the docs: [`guard/NOTES.md`](guard/NOTES.md) "Unverified" |
| (b) A `ChatMessage` round trip between the local Router and a hosted agent | **not run yet** | | |
| (c) A hosted KB write survives an agent restart | **not run yet** | | needs Person 1's hosted Vendor Agent |

Local check so far (2026-10-17): `python -m router.agent` starts on uAgents 0.26.1, registers on the Almanac API and publishes the Chat Protocol manifest.

## Known limits

Deferred on purpose, see [`TODOS.md`](TODOS.md) and "NOT in scope" in the design doc:
- ASI:One users get a team or role when no answer is found, not a later follow-up in the chat.
- No Slack file-drop ingestion. Documents are seeded with the CLI.
- Learned answers don't go stale when a source document changes.
- Expert names aren't hidden from external askers yet, and KB entries have no audience field.

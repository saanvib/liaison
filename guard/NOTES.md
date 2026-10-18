# Compliance Guard: notes

## Run
```
pip install -r contracts/requirements.txt -r guard/requirements.txt -r requirements-dev.txt
pytest guard                                   # offline, no network
python -m guard.eval.run_eval                  # offline eval: approving fake judge, overlap grounding
python -m guard.smoke                          # agent answers screen/draft/egress in-process (~40-70 s, see below)
GUARD_SEED=... ALLOWED_SENDERS=... python -m guard.agent
```

## Environment variables (`guard/.env.example`)
| Variable | Purpose |
|---|---|
| `GUARD_SEED` | Seed for the Guard's stable address. Required. |
| `ALLOWED_SENDERS` | Comma-separated Vendor and Router addresses (D14). Others are logged and dropped; empty drops all. |
| `LLM_API_KEY` | Enables the LLM judge and LLM grounding. Unset: judge unavailable, drafts that pass the rules are `needs_review`. |
| `LLM_MODEL` | Default `claude-sonnet-5-5` (A9). |
| `GUARD_LLM_TIMEOUT_S` | Per LLM call, default 4. A draft check makes up to two calls inside the Vendor's 10 s wait (D4). |
| `GUARD_PORT` | Local port, default 8001. |

## Eval with the real judge
```
LLM_API_KEY=... python -m guard.eval.run_eval --llm real
```
Exit 0 = bar met (100% recall per blocker category, overall precision >= 80%), 1 = bar missed, 2 = no key.
Costs a few cents (two short LLM calls per draft/egress row that passes the rules). Not run yet.

## Behavior worth knowing
- Mode comes from the request (A6): empty draft = screen (patient data only, set by `screen_rules` in `policy.json`); `VENDOR_AGENT` draft = rules, judge, grounding; `EXPERT` draft = rules, judge.
- A rule hit is final. The judge only tightens. Any judge failure is `needs_review` / "judge unavailable" (category `none`).
- Every rule scans both question and draft (`scan` in `policy.json`). Negated RUO disclaimers are stripped before matching.
- Grounding (offline): a sentence fails if under 50% of its content words appear in the cited snippets, or if it states a number the snippets don't.
- The real LLM client sends `fallbacks: "default"` (server-side refusal fallback); a remaining refusal fails closed.

## Unverified (hosted Agentverse, A11)
Checked against docs.agentverse.ai on 2026-10-17; not yet tested with a real deploy (Person 3, step 6):
- Multiple files: supported by the hosted editor. Folders (`guard/`, `contracts/` as packages) are not confirmed; if only flat files work, flatten at deploy time (A11) and tell the team.
- `anthropic` is on the allowed-imports list, but its version isn't stated. `llm.py` needs a 1.x SDK that accepts `output_config` and `fallbacks`; an older SDK raises, which fails closed (every draft that passes the rules becomes `needs_review`).
- Reading `policy.json` from disk is not confirmed. If file reads are blocked, the policy has to be inlined as a Python dict at deploy time.
- Hosted agents get a predefined `agent` object, and `Bureau` is not allowed. `create_agent()` builds its own `Agent`, so the hosted entry point must instead register `handle_request` on the provided `agent`.
- uAgents version on hosted is not stated; local tests use 0.26.1.

## Local quirk
uAgents 0.26.1 updates the agent's status on Agentverse before running any `startup` handler; from this
machine that call took ~33 s. Message handling is not delayed, but anything triggered on startup is (hence the
smoke run's 90 s timeout). The smoke clients set `report_events=False` to skip a second slow lookup.

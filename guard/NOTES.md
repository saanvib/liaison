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
| `LLM_WORKSPACE_ID` | Required if the key isn't scoped to one workspace (the API returns 400 otherwise); sent as `anthropic-workspace-id`. |
| `LLM_MODEL` | Default `claude-sonnet-5-5` (A9). |
| `GUARD_LLM_TIMEOUT_S` | Per LLM call, default 4. A draft check makes up to two calls inside the Vendor's 10 s wait (D4). |
| `GUARD_PORT` | Local port, default 8001. |

## Eval with the real judge
```
LLM_API_KEY=... python -m guard.eval.run_eval --llm real
```
Exit 0 = bar met (100% recall per blocker category, overall precision >= 80%), 1 = bar missed, 2 = no key.
Costs a few cents (two short LLM calls per draft/egress row that passes the rules). Any "judge unavailable" row
makes it exit 1 with the API error, so a dead judge can't pass. First live run (2026-10-17): PASS, 13/13 calls OK,
overall precision 100%; `ungr-02` came back `off_label` instead of `ungrounded` (same verdict).

## Behavior worth knowing
- Mode comes from the request (A6): empty draft = screen (patient data only, set by `screen_rules` in `policy.json`); `VENDOR_AGENT` draft = rules, judge, grounding; `EXPERT` draft = rules, judge.
- A rule hit is final. The judge only tightens. Any judge failure is `needs_review` / "judge unavailable" (category `none`).
- Every rule scans both question and draft (`scan` in `policy.json`). Negated RUO disclaimers are stripped before matching.
- Grounding (offline): a sentence fails if under 50% of its content words appear in the cited snippets, or if it states a number the snippets don't.
- The real LLM client sends `fallbacks: "default"` (server-side refusal fallback); a remaining refusal fails closed.

## Hosted Agentverse (probed 2026-10-17, A11)
A throwaway probe agent on hosted Agentverse found:

| Question | Result |
|---|---|
| Versions | Python 3.10.12, uagents 0.25.5, uagents-core 0.4.9, anthropic 0.125.0, pydantic 2.8.2 |
| Agent object | Predefined as `agent`; Agentverse assigns the address (`GUARD_SEED` is unused hosted) |
| Second file | Imports work |
| Folders | Don't work (`No module named 'probe_pkg'`) |
| Data files | Not found: code runs from `/runners`, not next to your files |
| Secrets | Arrive as env vars |
| Outbound HTTPS | Works (`api.anthropic.com` reachable) |

So the hosted Guard is a flat build: `python -m guard.hosted.build` writes `guard/hosted/dist/guard/` (imports
rewritten, `policy.json` embedded as `guard_policy.py`, entry point from `guard/hosted/entry.py` that registers on
the predefined `agent`) and `guard/hosted/dist/smoke_client/`. `dist/` is gitignored; rebuild, never edit.
`guard/tests/test_hosted_build.py` runs the build in a separate interpreter with only the flat files importable.
The full suite also passes on Python 3.10 with the hosted package versions.

## Deploying to hosted Agentverse
1. `python -m guard.hosted.build`
2. Agentverse: Launch an agent, then **Generate Agent** with a minimal prompt ("a minimal agent that logs hello on
   startup"). Open the code editor (Build).
3. Replace `agent.py` with `dist/guard/agent.py`; create each other file in `dist/guard/` under the same name.
4. Secrets: `ALLOWED_SENDERS` (Vendor and Router addresses), `LLM_API_KEY`, and if needed `LLM_WORKSPACE_ID`,
   `LLM_MODEL`.
5. Run. The log should show `GUARD ready: address=... judge=on allowed_senders=N`. Send that address to Persons 1
   and 3 for `GUARD_ADDRESS`.
6. Optional end-to-end check: a second hosted agent from `dist/smoke_client/` with secret `GUARD_ADDRESS`. Its log
   prints its own address; add it to the Guard's `ALLOWED_SENDERS`, rerun both, and expect three `SMOKE` lines:
   screen `blocked/patient_data`, draft `approved` (judge on) or `needs_review` (judge off), egress
   `blocked/pricing_contract`. Remove the client's address from `ALLOWED_SENDERS` afterwards.

Still unverified hosted: whether globals reset between messages affects anything (the Guard keeps no state).

## Local quirk
uAgents 0.26.1 updates the agent's status on Agentverse before running any `startup` handler; from this
machine that call took ~33 s. Message handling is not delayed, but anything triggered on startup is (hence the
smoke run's 90 s timeout). The smoke clients set `report_events=False` to skip a second slow lookup.

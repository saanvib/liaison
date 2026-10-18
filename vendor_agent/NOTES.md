# Vendor Agent: notes

## Run

```
pip install -r contracts/requirements.txt -r vendor_agent/requirements.txt -r requirements-dev.txt
pytest contracts/tests vendor_agent              # offline, no network, no API cost
python -m vendor_agent.smoke                     # local wiring check, fake Guard/Router/LLM
set -a; . vendor_agent/.env; set +a; python -m vendor_agent.agent
```

## Seeding documents

```
python -m vendor_agent.ingest vendor_agent/docs_seed --direct
```

`--direct` calls `VendorCore.handle_ingest` in-process and writes to the same on-disk KeyValueStore
a later `python -m vendor_agent.agent` run reads (both are keyed by `VENDOR_SEED`'s derived
address), so ingesting once before starting the agent persists. Without `--direct`, it sends each
document over the network to `VENDOR_ADDRESS` instead — needs a running agent first.

`docs_seed/seed_questions.md` is team reference material (sample questions), not a product
document — `ingest.py` still reads it (doc_type `other`) if you ingest the whole folder, but it
shouldn't be treated as something askers would actually ask about verbatim.

## Environment variables (`.env.example`)

| Variable | Purpose |
|---|---|
| `ROUTER_ADDRESS` | Trusted internal sender (A1); who the Router's chat replies/escalations come from. |
| `GUARD_ADDRESS` | Where `GuardRequest`s are sent. |
| `INGEST_ADDRESS` | Trusted sender for a non-`--direct` `IngestDocument`, alongside `ROUTER_ADDRESS`. |
| `APPROVER_ALLOWLIST` | Comma-separated Slack IDs (A12); must equal `python -m experts.print_allowlist`'s output. |
| `GUARD_TIMEOUT_S` | Screen/draft/egress Guard call timeout, default 10 (D4). |
| `KB_MATCH_THRESHOLD` | Cosine similarity for a KB hit, default 0.85. |
| `LOW_CONFIDENCE` | Retrieval confidence floor before escalating, default 0.5. |
| `LLM_API_KEY`, `LLM_MODEL` | Gemini, via its REST API directly (no SDK — see "Hosted Agentverse limits" below), default model `gemini-3.6-flash`. |
| `EMBEDDINGS_URL`, `EMBEDDINGS_API_KEY`, `EMBEDDINGS_MODEL` | Optional non-Gemini embeddings endpoint (OpenAI/Voyage-shaped). Unset: falls back to Gemini embeddings using `LLM_API_KEY` if that's set, else the fully offline bag-of-words embedder. |
| `TEAM_ROLES` | Optional JSON override of the `Topic -> text` names used in `team_role_reply`. |
| `VENDOR_SEED` | This agent's identity. Pick any secret phrase; never commit it, never reuse a dev seed for a real deployment. |

## Unverified / anything you should know before relying on this

- **Provider choice:** this slice uses **Gemini**, not Claude — the shared interfaces doc's A9
  assumption names Claude/`claude-sonnet-5-5`. Switched to avoid Anthropic API costs; contained
  entirely to `clients.py`'s `LLMClient`/`GeminiEmbedder` (no other file is provider-specific).
  Worth a one-line heads-up to the team since it diverges from what's written there.
- **Offline embedder calibration:** `OfflineEmbedder` (used whenever no `EMBEDDINGS_URL` and no
  `LLM_API_KEY`/Gemini key is set) is a crude binary bag-of-words. Its cosine score for a short
  query against a long (~800-character) document chunk is bounded by roughly
  `sqrt(|query tokens| / |chunk tokens|)`, regardless of how well the words actually match — so
  even a near-verbatim question can legitimately score well below the default `LOW_CONFIDENCE`
  (0.5) against real, longer seed documents. Confirmed empirically against `docs_seed/`: a
  shelf-life question scored ~0.3 against the exact datasheet paragraph that answers it. This
  isn't a bug in the question flow; it's a real limitation of the free fallback. A real Gemini
  (or other) embeddings key gives much better retrieval; if running fully offline, consider
  lowering `LOW_CONFIDENCE` or expect more escalations than a real embedder would produce.
- **Hosted Agentverse limits (A11) — built and verified:** per Person 2's probe (`guard/NOTES.md`),
  hosted Agentverse only supports flat top-level `.py` files (subfolders fail with
  `No module named '...'`) and can't find data files next to code. `python -m
  vendor_agent.hosted.build` flattens `vendor_agent/` into `vendor_agent/hosted/dist/vendor/`
  (gitignored; never edit, rebuild) — same pattern as `guard/hosted/build.py`: rewrites
  `vendor_agent.X` imports to flat `vendor_agent_X` names, drops `from __future__ import
  annotations` (Python 3.10 hosted breaks dataclasses with it, same bug Person 2 hit), and refuses
  to build (`BuildError`) if any import or module-level `__file__` use wasn't caught.
  `docs_seed/*.md` is **not** embedded — seed a hosted agent over the network instead:
  `python -m vendor_agent.ingest vendor_agent/docs_seed` (no `--direct`) once its real address is
  known. `vendor_agent/tests/test_hosted_build.py` simulates the exact hosted module-loading
  behavior (no `__file__`, module absent from `sys.modules` while it loads) and passed under a
  real Python 3.10.15 venv pinned to the probed hosted versions (`uagents==0.25.5`,
  `uagents-core==0.4.9`, `pydantic==2.8.2`) — ingest, retrieval, and KB accept/lookup all ran
  correctly through the flattened modules. `VENDOR_SEED` is unused hosted (Agentverse assigns the
  address); the hosted entry point (`vendor_agent/hosted/entry.py`) uses `agent.storage` directly
  instead of a seed-derived `KeyValueStore`.
  **Still genuinely unverified:** this was only run in a local simulation, not on real Agentverse
  infrastructure. Deploying is: `python -m vendor_agent.hosted.build`, then paste each file in
  `dist/vendor/` into the hosted agent's editor under the same name (`agent.py` replaces the
  generated one), set the secrets below, run, and watch for `VENDOR ready: address=...` in the log.
- **Live ASI:One check:** not yet run — the hosted package above is ready to deploy; the actual
  live deployment and the 3 required ASI:One scenarios haven't happened yet.
- **`clock` and `router` constructor params on `VendorCore`:** accepted per the shared doc's
  exact signature but unused by `handle_question` itself — Step 5's agent wiring sends the chat
  reply and calls `router.send_escalation`, not `core.py` (confirmed by `handle_question`'s own
  "never talks to Slack" note in the role file).
- **Internal "escalated" reply wording:** the role file only specifies text for the external
  `team_role_reply`; the internal case reuses the generic `HOLDING_TEXT` as a placeholder. Revisit
  once Person 3's Slack UX for escalations is final.

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
| `LLM_API_KEY`, `LLM_MODEL` | Gemini (`google-genai`), default model `gemini-3.6-flash`. |
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
- **Hosted Agentverse limits (A11):** not yet verified whether a hosted Agentverse agent can hold
  all of `vendor_agent/`'s modules and the `google-genai` dependency. If it can't: stop and tell
  the team; do not switch hosting unilaterally.
- **Live ASI:One check:** not yet run — gated on Person 3's hosting verification (merge-order
  step 4 in the shared doc). Once run, the shared chat session URL and the Agentverse agent
  profile URL go here.
- **`clock` and `router` constructor params on `VendorCore`:** accepted per the shared doc's
  exact signature but unused by `handle_question` itself — Step 5's agent wiring sends the chat
  reply and calls `router.send_escalation`, not `core.py` (confirmed by `handle_question`'s own
  "never talks to Slack" note in the role file).
- **Internal "escalated" reply wording:** the role file only specifies text for the external
  `team_role_reply`; the internal case reuses the generic `HOLDING_TEXT` as a placeholder. Revisit
  once Person 3's Slack UX for escalations is final.

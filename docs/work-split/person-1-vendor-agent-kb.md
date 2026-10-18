# Person 1: Vendor Agent + KB (+ the one contract change)

**For the Claude session implementing this slice.** Read `docs/work-split/00-shared-interfaces.md` first. You own only these paths: `contracts/**`, `contracts/tests/**`, `vendor_agent/**`. Do not edit any other path. Do not implement anything from the "deferred" list. Estimated total: **~14 human hours**; the KB trust rules (Step 3) and the flow tests (Step 4) deserve the most care.

## Goal
The Vendor Agent is the company's front door. It answers questions from uploaded documents with citations, serves learned expert answers from a KB, checks every question and every outgoing answer with the Guard under the rules below, escalates when it can't answer, and accepts expert answers back into the KB only through a strict trust rule. It speaks the Chat Protocol (ASI:One for external users; the Slack bot as one more chat client for internal users).

## Build order, with estimates and "done when" for each step

### Step 0 (PR0, ~1.5 h, do this first and merge it alone)
- `contracts/messages.py`: change `ApprovedAnswer.guard_verdict` to `Optional[GuardVerdict] = None`. Update its comment: "None for expert answers (the Guard is not applied to them)". Change nothing else in the file.
- `contracts/README.md`: add one line under the table saying `ApprovedAnswer.guard_verdict` is Optional and why.
- `contracts/tests/test_contracts.py`: round-trip every message through `model_dump_json` and back, with enums intact; `ApprovedAnswer` valid with and without a verdict; list defaults not shared between instances; `Verdict`, `GuardCategory`, `Topic`, `Source`, `EscalationReason` string values unchanged (assert the exact set).
- **Done when:** `python -m pytest contracts/tests` passes and the PR touches only `contracts/`. Tell Persons 2 and 3 it's merged.

### Step 1 (~1 h): config, clients, texts, models
Create in `vendor_agent/`:
- `config.py`: a frozen dataclass `Config` loaded from env: `ROUTER_ADDRESS`, `GUARD_ADDRESS`, `INGEST_ADDRESS`, `APPROVER_ALLOWLIST` (comma-separated Slack user IDs), `GUARD_TIMEOUT_S` (default 10), `KB_MATCH_THRESHOLD` (default 0.85), `LOW_CONFIDENCE` (default 0.5), `LLM_API_KEY`, `LLM_MODEL` (default `claude-sonnet-5-5`), `EMBEDDINGS_URL`, `EMBEDDINGS_API_KEY`, `EMBEDDINGS_MODEL`, and `TEAM_ROLES` (a dict `Topic -> text`, defaults: rd "our R&D team", qa_ra "our Quality and Regulatory team", engineering "our Engineering team", sales_ops "our Sales Operations team").
- `clients.py`: the `GuardClient` and `RouterClient` Protocols exactly as in the shared file; an `LLMClient` class (methods `draft(question, hits) -> str`, `classify_topic(question) -> Topic`) and an `Embedder` class (`embed(texts) -> list[list[float]]`); an `OfflineEmbedder` (bag-of-words cosine) used when `EMBEDDINGS_URL` is unset. Every external call sits behind these classes so tests can inject fakes.
- `texts.py`: constants for the fixed replies: retry (asks the asker to retry with a different question because it could not be verified), refusal (cannot process questions that may contain patient information), holding (the question was routed to the team), and a function `team_role_reply(topic, config)` that names the team or role, never a person, and says nothing was sent.
- `models.py`: `VendorOutput(reply_text: str|None, outcome: str, escalation: Escalation|None, asker_ref: str|None)` with `outcome` one of `answered|refused|retry|holding|escalated`; `AcceptResult(accepted: bool, reason: str)`.
- `.env.example` listing every variable above plus `VENDOR_SEED`.
- **Done when:** everything imports; a unit test builds `Config` from a dict.

### Step 2 (~3 h): retrieval, synthetic documents, ingest
- `retrieval.py`: `Retriever(embedder, storage)`. `add_document(doc: IngestDocument)` splits `doc.text` into chunks (about 800 characters with 100 overlap), embeds them, stores them. `search(query, k=4) -> list[Hit]` where `Hit` has `text, doc_id, doc_title, location, score`. `confidence(hits) -> float` in 0..1 (use the top score; document how you normalise). `to_citations(hits) -> list[Citation]`. `storage` is a tiny `Storage` Protocol with `get(key)` and `set(key, value)` backed by a dict in tests and by `ctx.storage` in the agent.
- `docs_seed/`: write four synthetic documents for a made-up research-use-only (RUO) product (invent the name; use no real vendor data): a datasheet, an IFU (instructions for use), a validation report, and a FAQ, each 400-800 words. Include facts that answer common questions, and deliberately leave gaps so some realistic questions have no answer in the documents. Also write `docs_seed/seed_questions.md` listing 6 answerable, 6 unanswerable, and 6 risky questions (clinical/diagnostic use of an RUO product, off-label use, comparison to a competitor, pricing, roadmap, a question containing a fake patient name and date of birth) for the team to reuse. Person 2 owns the real labeled set; do not edit it.
- `ingest.py`: a CLI, `python -m vendor_agent.ingest <folder> [--direct]`, that reads `.md`/`.txt` files and builds `IngestDocument(source="cli", doc_type=<from filename>)`. Without `--direct` it sends them to `VENDOR_ADDRESS` from an agent whose address is `INGEST_ADDRESS`; with `--direct` it calls `VendorCore.handle_ingest` in-process (used by tests and local dev).
- **Done when:** `pytest vendor_agent/tests/test_retrieval.py` passes offline, and `--direct` ingest of `docs_seed/` makes a known question return the right chunk with a citation.

### Step 3 (~2.5 h): `kb.py`, the KB and the write rule (D14, D18, D19, D23)
`KnowledgeBase(storage, embedder, config)` with:
- `lookup(question) -> KBEntry | None`: embed the question, cosine against stored entries, return the best at or above `KB_MATCH_THRESHOLD`.
- `add_pending(escalation: Escalation, flagged: bool)` and `get_pending(escalation_id)`: record `escalation_id`, `flagged`, `question`, `asker_ref`, `created_at` (A7). Flagged escalations are recorded as pending but can never be written to the KB.
- `accept(msg: ApprovedAnswer, sender: str) -> AcceptResult`: check in this exact order and stop at the first failure, logging a structured line with the reason:
  1. `sender == config.ROUTER_ADDRESS`, else `untrusted_sender`.
  2. a pending record exists for `msg.escalation_id`, else `unknown_escalation`.
  3. if the escalation was already written, return `AcceptResult(True, "duplicate")` and do nothing (idempotent).
  4. the pending record is not flagged, else `flagged_not_stored`.
  5. verdict rule: `msg.guard_verdict` is `approved`, **or** it is `None` and `approved_by` parsed as `"<slack_user_id>|<display name>"` has an ID in `APPROVER_ALLOWLIST`; anything else (a `needs_review` or `blocked` verdict, a `None` verdict with a non-allowlisted or malformed approver) is rejected with `bad_verdict` or `approver_not_allowed`.
  6. write the entry (use the pending record's question, `msg.answer`, `msg.topic`, approver ID and display name, today's date, `created_at`) and embed the question. If storage raises, log at ERROR and return `AcceptResult(False, "storage_error")` (T10: never silent).
- `attribution_note(entry) -> str`: exactly `Expert answer (<display name>, <topic value>, <YYYY-MM-DD>)`.
- **Done when:** `pytest vendor_agent/tests/test_kb_accept.py` passes. It is table-driven and covers: approved accepted; `None` plus allowlisted accepted; `None` plus not-allowlisted rejected; `needs_review` rejected; `blocked` rejected; wrong sender rejected; unknown `escalation_id` rejected; duplicate is an idempotent no-op; flagged pending rejected; storage error returns `storage_error`; malformed `approved_by` rejected.

### Step 4 (~3.5 h): `core.py`, the question flow (D4, D7, D22, D23, D32, D33)
`VendorCore(kb, retriever, llm, guard, router, storage, config, clock)` with `handle_question(text, sender, asker_ref)`, `handle_approved_answer(msg, sender)` (delegates to `kb.accept`), `handle_ingest(doc, sender)` (checks `sender` is `INGEST_ADDRESS` or `ROUTER_ADDRESS`, else ignore and log). Implement `handle_question` as follows. `internal = (sender == ROUTER_ADDRESS)`; the failure counter key is `asker_ref` if given, else `sender`.

1. **Screen (rules-only Guard call).** Start the embedding of the question concurrently with the screen call. Send `GuardRequest(request_id=<uuid>, source=VENDOR_AGENT, question=text, draft_answer="")` with `guard.check(..., timeout_s=GUARD_TIMEOUT_S)`.
   - Verdict `blocked` (patient data): return `outcome=refused`, the refusal text, **no** LLM call, **no** escalation, nothing stored. Reset the failure counter.
   - `None` (no verdict in time): increment the counter. First miss: return `outcome=retry` with the retry text. A second consecutive miss: **flagged forward**. Internal: build `Escalation(escalation_id=<uuid>, question=text, reason=GUARD_NEEDS_REVIEW, topic=None, draft_answer=None, guard_verdict=None, asker_ref=asker_ref)` (this is the A5 "flagged" shape), `kb.add_pending(..., flagged=True)`, return `outcome=escalated` with that escalation. External: return `outcome=holding` using `team_role_reply` for a topic you classify, nothing sent. Reset the counter.
   - `approved`: reset the counter and continue.
2. **KB lookup.** `kb.lookup(text)`. On a hit: internal returns `answer + "\n\n" + attribution_note`, `outcome=answered`. External sends `GuardRequest(source=EXPERT, question=text, draft_answer=entry.answer, citations=[])` (A6 egress mode: rules, then judge); if approved return the answer with its note, otherwise (blocked, needs_review, or no verdict) return the `team_role_reply`, `outcome=holding`.
3. **Retrieve.** `hits = retriever.search(text)`; `conf = retriever.confidence(hits)`. If `conf < LOW_CONFIDENCE`: not found. Internal: `Escalation(reason=LOW_CONFIDENCE, topic=llm.classify_topic(text), draft_answer=None, guard_verdict=None, asker_ref=...)`, `kb.add_pending(..., flagged=False)`, `outcome=escalated`. External: `team_role_reply`, `outcome=holding`.
4. **Draft and Guard.** `draft = llm.draft(text, hits)`; send `GuardRequest(source=VENDOR_AGENT, question=text, draft_answer=draft, citations=to_citations(hits), confidence=conf)`.
   - `approved`: return `draft` with citations rendered, `outcome=answered`.
   - `None`: **never send the draft** (D4). Internal: `Escalation(reason=GUARD_NEEDS_REVIEW, draft_answer=draft, guard_verdict=None, topic=<classified>, ...)` (A5: not flagged because `draft_answer` is set), `outcome=escalated`. External: holding reply.
   - `needs_review` or `blocked`: internal escalates with `reason=GUARD_NEEDS_REVIEW` or `GUARD_BLOCKED`, the verdict and draft attached; if `verdict.reroute_topic` is set use it as the topic. External: `team_role_reply`.
5. **Reply plumbing.** `handle_question` never talks to Slack. It returns `VendorOutput`; the agent wiring (Step 5) sends the reply and, when `escalation` is set, `router.send_escalation`.
- **Done when:** `pytest vendor_agent/tests/test_flow.py` passes with fake `GuardClient`, `RouterClient`, `LLMClient`, `Embedder`: screen blocked (no LLM call, no escalation); first no-verdict then second no-verdict (retry, then flagged escalation for internal, holding for external); internal KB hit with attribution; external KB hit passes the Guard in `EXPERT` mode and is blocked in another case; draft approved; draft with no verdict (draft never in reply, escalation carries it); needs_review and blocked internal and external; low-confidence escalation; the counter resets after any successful screen; and a timing test showing the embedding runs concurrently with the screen.

### Step 5 (~1 h): `agent.py`, uAgents wiring (no business logic here)
- `Agent(name="liaison-vendor", seed=VENDOR_SEED, ...)`; the Chat Protocol: `from uagents_core.contrib.protocols.chat import ChatMessage, ChatAcknowledgement, TextContent, chat_protocol_spec`, `chat_proto = Protocol(spec=chat_protocol_spec)`. Verified on uagents 0.26.1: `ChatMessage` has `timestamp`, `msg_id`, `content`; `ChatAcknowledgement` has `timestamp`, `acknowledged_msg_id`, `metadata`.
- On `ChatMessage`: send a `ChatAcknowledgement` first. Take the text. If `sender == ROUTER_ADDRESS`, parse the A2 envelope (`LIAISON-REF: <asker_ref>\n\n<question>`); otherwise the whole text is the question and `asker_ref = f"chat:{sender}"`. Call `core.handle_question`. Reply with a `ChatMessage`: to the Router, use the reply envelope (`LIAISON-REF`, `OUTCOME`, blank line, body) and, when `output.escalation` is set, also call `router.send_escalation`; to external users, plain text only.
- `@agent.on_message(ApprovedAnswer)` and `@agent.on_message(IngestDocument)` call the core; `@agent.on_message(GuardVerdict)` completes the pending `Future` for that `request_id`.
- Implement `UAgentGuardClient` (sends `GuardRequest` to `GUARD_ADDRESS`, awaits a `Future` keyed by `request_id`, returns `None` on timeout) and `UAgentRouterClient` (sends `Escalation` and chat replies to `ROUTER_ADDRESS`).
- Include the chat protocol with `publish_manifest=True` and a short `README.md` for the Agentverse profile.
- **Done when:** the agent starts locally with the fake env, registers, and answers a hand-sent `ChatMessage` against the seeded docs. **If a hosted Agentverse agent can't hold several modules or the needed packages (A11, shared file), stop and tell the team; do not switch hosting yourself.**

## Do not
- Do not edit `guard/`, `experts/`, `router/`, `tests/`, `README.md`, `docs/designs/liaison.md`, `TODOS.md`.
- Do not add contract messages or fields beyond the one in Step 0.
- Do not build staleness tracking, async follow-up for ASI:One, or Slack file-drop ingestion.
- Do not call any network service from a test.

## Definition of done for the whole slice
`pytest contracts/tests vendor_agent` is green offline; the agent starts and handles a chat message end to end against fakes; every rule above has a test; a short `vendor_agent/NOTES.md` lists the env vars, how to seed documents, and anything unverified (hosted limits).

# Liaison work split: shared interfaces (read first, everyone)

Source of truth: `docs/designs/liaison.md` (original plan, the decision ledger R1-R25, and "Accepted plan amendments"). This file turns those decisions into a build split for three people so they can work in parallel with almost no merge conflicts. **Nothing here is implemented yet. Each person implements only their own file.**

Files in this folder: `person-1-vendor-agent-kb.md`, `person-2-guard-experts-tests.md`, `person-3-router-slack.md`. Treat all four files in `docs/work-split/` as read-only. If something here is wrong, tell the team and change it by agreement in one small PR.

## How an implementing Claude session must use these files
1. Read this file, then your own person file, completely, before writing code.
2. Edit **only** the paths your person file lists as owned. If a task seems to need an edit elsewhere, stop and ask the human.
3. Never import from another person's directory, except `contracts` (everyone) and the public `experts` API (Router only, see below). Dependencies on other agents go through the injectable interfaces defined below, so you can build and test with fakes.
4. All LLM and embedding calls go through a small injectable client class. Tests inject fakes. **No test may touch the network.**
5. Implement in the order given. Run the stated checks. Commit small, with the prefix `[P1]`, `[P2]` or `[P3]`. `git pull --rebase origin main` before every push.
6. If a decision here is ambiguous, do not invent one silently: write the question in your PR description and ask the human.

## Ownership map (no overlap)

| Path | Owner | Notes |
|---|---|---|
| `contracts/**`, `contracts/tests/**` | Person 1 | one small change (PR0), then frozen |
| `vendor_agent/**` | Person 1 | |
| `guard/**` | Person 2 | |
| `experts/**` | Person 2 | public API used by Router |
| `tests/**`, `pytest.ini`, `requirements-dev.txt` | Person 2 | root loop test and shared pytest setup |
| `router/**` | Person 3 | |
| `README.md`, `scripts/**` | Person 3 | |
| `docs/designs/liaison.md`, `TODOS.md`, `docs/work-split/**` | nobody | read-only |

Each owner keeps their own `requirements.txt` and `.env.example` inside their directory. `contracts/requirements.txt` is Person 1's.

## Merge order
1. **PR0 (Person 1, within the first hour, tiny):** `contracts/messages.py` makes `ApprovedAnswer.guard_verdict` Optional (`Optional[GuardVerdict] = None`), plus a contract round-trip test and a README note. Person 2 and Person 3 start on work that does not need it and rebase when it merges.
2. **Parallel:** Person 1 `vendor_agent/`, Person 2 `guard/` and `experts/`, Person 3 `router/`. No shared files.
3. **Last:** Person 2's `tests/` loop test (needs the three `core.py` modules; builds against fakes first and swaps in the real cores as they merge).
4. **Before anything is deployed:** the verification in Person 3's step 6 (hosted limits, Chat Protocol link).

## Team load (human hours, estimates)
| | Person 1 | Person 2 | Person 3 |
|---|---|---|---|
| Hours | ~14 | ~14 | ~13 |
| Hardest part | hosted agent, KB write trust, Guard-timeout flows | Guard rules/judge/eval, expert matching | Slack flows, approval and reply state machine |

## Decisions everyone must honor (plain language; D/R numbers refer to the ledger)
- **Channels (D25, D30 reply, D31):** Slack is the UI for internal employees. ASI:One and the Chat Protocol serve external customers. The Slack bot (Router) talks to the Vendor Agent over the Chat Protocol as one more chat client. **No new contract messages.**
- **Runtime (D6, D26):** uAgents on Agentverse. Vendor Agent and Guard hosted, Router a local mailbox agent. Hosted limits are unverified (see step 6 of Person 3 and each person's "if hosted blocks you" note).
- **Front door rules (D4, D7, D22, D23):**
  - Every question is first sent to the Guard for a patient-data screen.
  - If the Guard returns no verdict within **10 s**, the Vendor Agent never sends a draft. At the screen step the first miss tells the asker to retry; after one re-attempt that also has no verdict the request goes forward on the referral path with a "potentially classified information" flag. A flagged question skips LLM expert matching and topic classification, goes to a fixed fallback, and is never stored as a KB key.
- **No answer found:** internal (Slack): the bot names the matched expert and sends the question only after the **asker approves** (R16, D10). External (ASI:One): the reply names a **team or role in text, not a person**, and sends nothing (D32).
- **Experts (D17, D20, D19):** expert replies skip the Guard. For internal users the KB serves stored answers as stored, with a visible note `Expert answer (<display name>, <topic>, <YYYY-MM-DD>)`. **External askers only get answers that passed the Guard** (D33), including KB-served ones.
- **KB write (D14, D18):** accept an `ApprovedAnswer` only from the Router's address, only for a pending `escalation_id`, only if `guard_verdict` is `approved`, or is `None` with an allowlisted approver; reject everything else; writes are idempotent.
- **Slack rules (D8, D11):** first non-bot reply in the escalation thread wins; later replies and edits are ignored; one reminder after 15 min; only the matched expert plus the per-topic allowlist may reply or click.
- **Tests (D12, D13):** pytest; Guard eval is pass/fail: recall 100% per blocker category on the labeled set and overall precision at least 80%.
- **Deferred, do not build:** async follow-up in ASI:One chat (D1/D32), Slack file-drop ingestion (D2), stale-answer invalidation (D24), hiding expert names for external askers and an audience field (TODOS.md).

## Interface agreements (frozen here; change only by agreement)

### The contract
`from contracts import ...` with the repo root on `PYTHONPATH`. After PR0 the only change is `ApprovedAnswer.guard_verdict: Optional[GuardVerdict] = None`. `GuardRequest`, `GuardVerdict`, `Escalation`, `IngestDocument`, `Citation` and the enums are unchanged.

### Conventions without schema changes (assumptions of this split, confirm before coding)
- **A1 Audience:** the Vendor Agent treats a sender as **internal iff its address equals `ROUTER_ADDRESS`**; every other sender is external.
- **A2 Slack envelope (Router to Vendor):** because `ChatMessage` only has `timestamp`, `msg_id` and `content`, the Router sends text of the form `LIAISON-REF: <asker_ref>\n\n<question>`. For replies to the Router the Vendor sends `LIAISON-REF: <asker_ref>\nOUTCOME: <answered|refused|retry|holding|escalated>\n\n<body>`. External ASI:One users send and receive plain text with no envelope.
- **A3 `asker_ref` format:** Slack `slack:<channel_id>:<thread_ts>:<user_id>`; ASI:One `chat:<sender_address>`.
- **A4 `approved_by`:** `"<slack_user_id>|<display name>"`. The Vendor Agent splits on the first `|`, checks the ID against `APPROVER_ALLOWLIST`, and uses the display name in the attribution note.
- **A5 Flagged escalation:** `Escalation.guard_verdict is None` **and** `draft_answer is None` **and** `reason == GUARD_NEEDS_REVIEW` means "flagged: potentially classified information". A draft-phase Guard timeout (D4) is the same except `draft_answer` is set. `topic` is `None` when flagged.
- **A6 Guard modes** (by the content of the `GuardRequest`):
  - *screen*: `draft_answer == ""`, `source == VENDOR_AGENT`. Rules only (patient data, injection-looking input). No judge, no grounding.
  - *draft*: non-empty `draft_answer`, `source == VENDOR_AGENT`. Rules, then judge, then grounding against `citations`.
  - *egress for KB or expert text* (external askers, D33): `source == EXPERT`, `citations` empty. Rules, then judge. **No grounding.**
- **A7 Pending escalations:** the Vendor Agent stores every escalation it sends (`escalation_id`, `flagged`, `question`, `asker_ref`, `created_at`) in its KB storage so it can check an `ApprovedAnswer` against it.
- **A8 Router waits on the Vendor Agent at most 30 s** (T10): then the asker gets an error message.
- **A9 Providers:** LLM via `LLM_API_KEY` and `LLM_MODEL` (default `claude-sonnet-5-5`), embeddings via `EMBEDDINGS_URL`, `EMBEDDINGS_API_KEY`, `EMBEDDINGS_MODEL`. If the embeddings variables are unset, fall back to an offline bag-of-words similarity so dev and tests run without a network.
- **A10 Addresses and seeds:** env vars `VENDOR_ADDRESS`, `GUARD_ADDRESS`, `ROUTER_ADDRESS`, `VENDOR_SEED`, `GUARD_SEED`, `ROUTER_SEED`. Each person documents the ones they use in their own `.env.example`.
- **A12 One approver list:** the Vendor Agent reads `APPROVER_ALLOWLIST` (env, comma-separated Slack IDs) so it never imports from `experts/`. That list must equal the Slack IDs in `experts/directory.yaml`; Person 2 provides `python -m experts.print_allowlist`, which prints the exact env line, and Person 3 pastes it into the setup docs.
- **A11 Hosted layout is unverified:** the plan assumes a hosted agent can hold several modules. If it can't, flatten at deploy time only, and tell the team; never change a decision silently.

### Injectable interfaces (each person implements their side; the others use fakes)
```python
# Person 1 defines (vendor_agent/clients.py); Person 2's loop test and Person 3's tests implement fakes of it.
class GuardClient(Protocol):
    async def check(self, req: GuardRequest, timeout_s: float = 10.0) -> GuardVerdict | None: ...  # None = no verdict in time

class RouterClient(Protocol):
    async def send_escalation(self, esc: Escalation) -> None: ...
    async def reply_chat(self, to_sender: str, text: str) -> None: ...

# Person 2 defines (guard/core.py)
class GuardCore:
    def check(self, req: GuardRequest) -> GuardVerdict: ...          # sync, pure, no network except the injected LLM client

# Person 2 defines (experts/__init__.py) -- public API, used by Router agent wiring only
@dataclass
class Expert: id: str; slack_user_id: str; display_name: str; role: str; topics: list[Topic]; summary: str
@dataclass
class MatchResult: expert: Expert | None; topic: Topic; score: float; fallback: bool   # fallback=True means "use the topic channel"
def match(question: str, topic: Topic | None = None) -> MatchResult: ...
def load_directory() -> list[Expert]: ...
def allowlist(topic: Topic) -> set[str]: ...                          # Slack user IDs allowed for that topic channel

# Person 1 defines (vendor_agent/core.py), Person 3 consumes via the Chat Protocol only
class VendorCore:
    async def handle_question(self, text: str, sender: str, asker_ref: str | None) -> "VendorOutput": ...
    async def handle_approved_answer(self, msg: ApprovedAnswer, sender: str) -> "AcceptResult": ...
    async def handle_ingest(self, doc: IngestDocument, sender: str) -> None: ...
# Person 3 defines (router/core.py)
class RouterCore: ...   # see person-3 file; takes injected matcher, vendor client, slack client, clock
```

### Done together (milestones)
- **M1 (about hour 4):** each `core.py` merged with fakes; `pytest` green in each directory.
- **M2 (about hour 9):** `agent.py` wiring done for all three; each agent starts locally.
- **M3 (about hour 13):** Person 2's loop test and the Guard eval pass on `main`; Person 3's verification note is in the README.

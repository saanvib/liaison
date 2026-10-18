# TODOs

Accepted during /plan-eng-review of docs/designs/liaison.md (2026-10-17).

## Mark learned answers stale when their source documents change
**What:** Store `created_at`, `topic` and the doc ids cited at escalation time on each learned KB entry; when a document is re-ingested, mark entries citing it stale (not served, re-escalated).
**Why:** Instant answers shouldn't outlive the datasheet they came from. Deferred by D24 as out of scope for the first build.
**Pros:** Closes the stale-answer gap without changing the contract.
**Cons:** Two KB fields and a re-ingest check; entries with no cited documents never go stale.
**Context:** `Escalation.citations` already carries the cited docs (contracts/messages.py:82-90). Ledger: R14 and R21 in docs/designs/liaison.md.
**Depends on / blocked by:** The KB write path and the ingest CLI existing.

## Hide expert names from external askers and add an audience field to KB entries
**What:** Answers sent to external ASI:One users should show the expert's team or role instead of the display name (D19 shows a display name today, which conflicts with D32's no-names rule for external askers), and KB entries should carry an audience (internal or external) so the serve path can branch cleanly (D33).
**Why:** Expert answers skip the Guard for internal Slack users (D17, D20), and external answers are Guard-checked (D33). Staff names and an implicit audience are the remaining gaps for the external channel. Narrowed from the original D28 TODO after external customers came back via ASI:One.
**Pros:** A privacy-safe external channel and a KB that knows who may see what.
**Cons:** A new KB field and a small change to the attribution note.
**Context:** D17, D19, D20, D32, D33; `ApprovedAnswer` has no audience field (contracts/messages.py:93-101). Ledger: R22 and R25 in docs/designs/liaison.md.
**Depends on / blocked by:** The internal Slack version and the ASI:One path both working.

## Ingest documents from a Slack file drop
**What:** A Slack `file_shared` handler that downloads the file, extracts text and sends `IngestDocument`.
**Why:** Lets people add documents without a command line. Deferred by D2; documents are seeded by a CLI for now.
**Pros:** A live "drop a datasheet and ask about it" moment.
**Cons:** Slack file permissions and PDF text extraction in the busiest agent.
**Context:** D2; `IngestDocument` (contracts/messages.py:104-112). Ledger: R23 in docs/designs/liaison.md.
**Depends on / blocked by:** The Slack app and the ingest path working.

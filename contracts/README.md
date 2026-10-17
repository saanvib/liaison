# contracts/ (frozen after kickoff)

Shared messages for Liaison. Import with `from contracts import GuardRequest, GuardVerdict, ...`.
Install: `pip install -r contracts/requirements.txt`. Each owner's directory pulls this in alongside its own requirements.

| Message | Sender -> Receiver | Notes |
|---|---|---|
| GuardRequest | Vendor Agent / Router -> Guard | `source` says who wrote the draft |
| GuardVerdict | Guard -> sender | approved / needs_review / blocked, with `category` and `reason`; `reroute_topic` for pricing |
| Escalation | Vendor Agent -> Router | low confidence or non-approved verdict |
| ApprovedAnswer | Router -> Vendor Agent | expert answer, Guard-approved; Vendor Agent writes it to the KB |
| IngestDocument | Router -> Vendor Agent | doc dropped in #liaison-docs |

Ownership: Person 1 `vendor_agent/`, Person 2 `guard/`, Person 3 `router/` + root README. Nobody edits another's directory; changes to `contracts/` need all three.

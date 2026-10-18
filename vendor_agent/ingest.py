"""CLI to ingest .md/.txt documents into the Vendor Agent.

    python -m vendor_agent.ingest <folder> [--direct]

Without --direct, sends each file as an IngestDocument over the network to
VENDOR_ADDRESS, from a short-lived agent seeded as INGEST_SEED (A10). With
--direct, calls VendorCore.handle_ingest in-process — used by tests and
local dev, no network involved.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

from contracts import IngestDocument

_DOC_TYPES = {"datasheet", "ifu", "validation_report", "faq"}


def _doc_type_from_filename(path: Path) -> str:
    stem = path.stem.lower()
    return stem if stem in _DOC_TYPES else "other"


def load_documents(folder: Path) -> list[IngestDocument]:
    """Read every .md/.txt file in `folder` into an IngestDocument. doc_type is
    guessed from the filename (datasheet/ifu/validation_report/faq, else "other")."""
    docs = []
    for path in sorted(Path(folder).iterdir()):
        if path.suffix.lower() not in (".md", ".txt"):
            continue
        docs.append(
            IngestDocument(
                doc_id=str(uuid.uuid4()),
                title=path.stem.replace("_", " ").title(),
                doc_type=_doc_type_from_filename(path),
                text=path.read_text(encoding="utf-8"),
                filename=path.name,
                source="cli",
            )
        )
    return docs


async def ingest_direct(docs: list[IngestDocument], core, sender: str) -> None:
    """Call VendorCore.handle_ingest in-process for each document."""
    for doc in docs:
        await core.handle_ingest(doc, sender)


async def ingest_over_network(docs: list[IngestDocument], vendor_address: str, ingest_seed: str) -> int:
    """Send each document to `vendor_address` from a short-lived agent. Returns
    the number of documents the network reported as delivered."""
    from uagents import Agent, Context

    agent = Agent(name="vendor-ingest-cli", seed=ingest_seed, report_events=False, mark_inactive_on_shutdown=False)
    done = asyncio.Event()
    statuses = []

    @agent.on_event("startup")
    async def _send(ctx: Context):
        for doc in docs:
            statuses.append(await ctx.send(vendor_address, doc))
        done.set()

    task = asyncio.create_task(agent.run_async())
    try:
        await asyncio.wait_for(done.wait(), timeout=30.0)
    finally:
        task.cancel()
    return sum(1 for s in statuses if getattr(s.status, "value", None) == "delivered")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest .md/.txt documents into the Vendor Agent")
    parser.add_argument("folder", type=Path)
    parser.add_argument(
        "--direct", action="store_true",
        help="call VendorCore.handle_ingest in-process instead of sending over the network",
    )
    args = parser.parse_args(argv)

    docs = load_documents(args.folder)
    if not docs:
        print(f"no .md/.txt files found in {args.folder}", file=sys.stderr)
        return 1

    if args.direct:
        from vendor_agent.agent import build_core  # deferred: only needed for --direct (Step 5)

        core = build_core()
        asyncio.run(ingest_direct(docs, core, sender=os.environ["INGEST_ADDRESS"]))
        print(f"ingested {len(docs)} document(s) directly")
        return 0

    vendor_address = os.environ["VENDOR_ADDRESS"]
    ingest_seed = os.environ.get("INGEST_SEED", "liaison-dev-only ingest cli")
    delivered = asyncio.run(ingest_over_network(docs, vendor_address, ingest_seed))
    print(f"sent {len(docs)} document(s), {delivered} delivered")
    return 0 if delivered == len(docs) else 1


if __name__ == "__main__":
    sys.exit(main())

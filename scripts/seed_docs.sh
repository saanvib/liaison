#!/usr/bin/env bash
# Seed the synthetic documents into the Vendor Agent's KB in-process (Person 1's ingest CLI).
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -f vendor_agent/ingest.py ]; then
  echo "vendor_agent/ingest.py is not in this checkout yet (Person 1). Pull main and try again." >&2
  exit 1
fi
exec "${PYTHON:-python}" -m vendor_agent.ingest vendor_agent/docs_seed --direct

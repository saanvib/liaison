"""Run the Vendor Agent on this laptop with a local endpoint, for an end-to-end dev test.

    set -a; . vendor_agent/.env; set +a; python scripts/dev_vendor_local.py

`python -m vendor_agent.agent` starts the agent with no endpoint, so a local Router can't
reach it. This only adds port and endpoint (VENDOR_PORT, default 8003); all logic is
vendor_agent's own. The hosted Vendor on Agentverse doesn't use this.
"""
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vendor_agent.agent import create_agent  # noqa: E402

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("VENDOR_PORT", 8003))
    vendor, _ = create_agent(port=port, endpoint=[f"http://127.0.0.1:{port}/submit"])
    print(f"Vendor address: {vendor.address}")
    vendor.run()

#!/usr/bin/env bash
# Dev run: start the local Liaison agents, each with its own <dir>/.env.
#   scripts/run_all.sh            # Guard, Vendor Agent and Router all local
#   HOSTED=1 scripts/run_all.sh   # Router only (Vendor Agent and Guard run hosted on Agentverse)
# Logs go to $LOG_DIR (default: a temp folder). Ctrl+C stops everything.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python}"
LOG_DIR="${LOG_DIR:-${TMPDIR:-/tmp}/liaison-logs}"
mkdir -p "$LOG_DIR"
# The python.org macOS build ships without a CA bundle; certifi's works for Agentverse and Slack.
export SSL_CERT_FILE="${SSL_CERT_FILE:-$("$PY" -m certifi 2>/dev/null || true)}"
[ -n "$SSL_CERT_FILE" ] || unset SSL_CERT_FILE

pids=()
cleanup() { [ ${#pids[@]} -gt 0 ] && kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

start() {  # start <name> <module or script.py> <env file>
  local name=$1 module=$2 envfile=$3 file run
  if [[ "$module" == *.py ]]; then file=$module; run=("$PY" "$module"); else file="${module//.//}.py"; run=("$PY" -m "$module"); fi
  if [ ! -f "$file" ]; then echo "skip $name: $module is not in this checkout yet"; return 0; fi
  if [ ! -f "$envfile" ]; then echo "skip $name: $envfile missing (copy $envfile.example and fill it in)"; return 0; fi
  ( set -a; . "$envfile"; set +a; exec "${run[@]}" ) >"$LOG_DIR/$name.log" 2>&1 &
  pids+=($!)
  echo "started $name (pid $!), log: $LOG_DIR/$name.log"
}

if [ -z "${HOSTED:-}" ]; then
  start guard guard.agent guard/.env
  start vendor scripts/dev_vendor_local.py vendor_agent/.env  # adds a local endpoint so the Router can reach it
fi
start router router.agent router/.env

cat <<'EOF'

Reminders:
- Addresses must line up: router VENDOR_ADDRESS = the Vendor's address; the Vendor's ROUTER_ADDRESS and the
  Guard's ALLOWED_SENDERS include the Router's address (each agent prints its address on start; see the logs).
- The Vendor's APPROVER_ALLOWLIST = the output of: python -m experts.print_allowlist
- Hosted agents (HOSTED=1): deploy vendor_agent/ and guard/ on Agentverse first, and check the README "Verification" table.
- First Router start: open the "Agent inspector" link in the router log and connect the mailbox.
- Seed documents: scripts/seed_docs.sh
EOF

if [ ${#pids[@]} -eq 0 ]; then
  echo; echo "Nothing started (see the skip lines above)."
  exit 0
fi
wait

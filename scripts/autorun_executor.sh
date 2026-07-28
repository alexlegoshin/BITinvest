#!/usr/bin/env bash
# Runs on the slave/executor host. Reads the slave account token, fetches
# step.json published by the parser host, and places rebalancing orders.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"

mkdir -p secrets data

# Exactly one slave token: orders are computed against the whole slave
# portfolio, so a second token would replay the same orders and double them.
if [ ! -s secrets/slave_tokens.txt ]; then
  read -rsp "Enter the slave account token: " token; echo
  [ -z "$token" ] && { echo "no token given" >&2; exit 1; }
  echo "$token" > secrets/slave_tokens.txt
  echo "1.0" > secrets/slave_weights.txt
  chmod 600 secrets/slave_tokens.txt secrets/slave_weights.txt
fi

: "${BITINVEST_STEP_URL:?set BITINVEST_STEP_URL to the parser hosts step.json, e.g. http://legoshi.pro:8082/step.json}"

while true; do
  if wget -q "$BITINVEST_STEP_URL" -O data/step.json; then
    python3 -m bitinvest.executor_service || echo "executor_service failed" >&2
  else
    echo "could not fetch step.json from $BITINVEST_STEP_URL" >&2
  fi
  sleep 60
done

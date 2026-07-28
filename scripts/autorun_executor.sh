#!/usr/bin/env bash
# Runs on the slave/executor host. Reads the slave account token, fetches
# step.csv published by the parser host, and places rebalancing orders.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"

mkdir -p secrets data

if [ ! -s secrets/slave_tokens.txt ]; then
  : > secrets/slave_tokens.txt
  : > secrets/slave_weights.txt
  i=1
  while true; do
    read -rsp "Enter slave token $i (empty to stop): " token; echo
    [ -z "$token" ] && break
    read -rsp "Enter weight of slave token $i: " weight; echo
    echo "$token" >> secrets/slave_tokens.txt
    echo "$weight" >> secrets/slave_weights.txt
    i=$((i + 1))
  done
  chmod 600 secrets/slave_tokens.txt secrets/slave_weights.txt
fi

: "${BITINVEST_STEP_CSV_URL:?set BITINVEST_STEP_CSV_URL to the parser hosts step.csv, e.g. http://legoshi.pro:8082/step.csv}"

while true; do
  if wget -q "$BITINVEST_STEP_CSV_URL" -O data/step.csv; then
    python3 -m bitinvest.executor_service || echo "executor_service failed" >&2
  else
    echo "could not fetch step.csv from $BITINVEST_STEP_CSV_URL" >&2
  fi
  sleep 60
done

#!/usr/bin/env bash
# Runs on the master/parser host. Reads master account tokens, republishes
# step.json for the executor host to fetch over HTTP.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"

mkdir -p secrets data

if [ ! -s secrets/master_tokens.txt ]; then
  : > secrets/master_tokens.txt
  : > secrets/master_weights.txt
  i=1
  while true; do
    read -rsp "Enter master token $i (empty to stop): " token; echo
    [ -z "$token" ] && break
    read -rsp "Enter weight of master token $i: " weight; echo
    echo "$token" >> secrets/master_tokens.txt
    echo "$weight" >> secrets/master_weights.txt
    i=$((i + 1))
  done
  chmod 600 secrets/master_tokens.txt secrets/master_weights.txt
fi

PUBLISH_DIR=${BITINVEST_PUBLISH_DIR:-/data/www}
mkdir -p "$PUBLISH_DIR"

while true; do
  if python3 -m bitinvest.parser_service; then
    cp data/step.json "$PUBLISH_DIR/step.json"
  else
    echo "parser_service failed, will retry" >&2
  fi
  sleep 60
done

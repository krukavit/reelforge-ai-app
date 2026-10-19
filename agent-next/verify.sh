#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

echo '=== COMPILE ==='
python3 -m compileall -q app tests agent.py

echo '=== LOCAL TESTS ==='
python3 -m tests.run_all

echo '=== AGENT SELF-TEST ==='
python3 agent.py self-test

echo '=== REGISTRY ==='
python3 agent.py registry >/dev/null

echo 'VERIFY_OK'

#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

# Offline verification needs a config file for import-time MCPClient setup.
# Never overwrite a local installation config; remove only a file created here.
CI_CREATED_MCP_CONFIG=0
cleanup_ci_mcp_config() {
  if [ "$CI_CREATED_MCP_CONFIG" -eq 1 ]; then
    rm -f -- "$ROOT/config/mcp.txt"
  fi
}
trap cleanup_ci_mcp_config EXIT

if [ ! -f "$ROOT/config/mcp.txt" ]; then
  if [ ! -f "$ROOT/config/mcp.example.txt" ]; then
    echo "VERIFY_ERROR: config/mcp.example.txt is missing" >&2
    exit 1
  fi
  CI_CREATED_MCP_CONFIG=1
  cp -- "$ROOT/config/mcp.example.txt" "$ROOT/config/mcp.txt"
fi

echo '=== COMPILE ==='
python3 -m compileall -q app tests agent.py

echo '=== LOCAL TESTS ==='
python3 -m tests.run_all

echo '=== AGENT SELF-TEST ==='
python3 agent.py self-test

echo '=== REGISTRY ==='
python3 agent.py registry >/dev/null

echo 'VERIFY_OK'

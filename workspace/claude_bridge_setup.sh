#!/bin/zsh
# Sets up the Python venv the Claude Code bridge (claude_worker.py) runs in: agent-ide/workspace/.claude-bridge/venv,
# gitignored. server.py itself stays stdlib-only; without this venv the page keeps Claude Code sessions watch-only.
# Usage: zsh agent-ide/workspace/claude_bridge_setup.sh [python3.10+]
set -euo pipefail
cd -- "${0:A:h}"
PY="${1:-}"
if [[ -z "$PY" ]]; then
  for c in python3.13 python3.12 python3.11 python3.10 /usr/local/bin/python3 python3; do
    if command -v "$c" >/dev/null && "$c" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then PY="$c"; break; fi
  done
fi
[[ -n "$PY" ]] || { echo "Needs Python 3.10 or newer (pass its path as the first argument)." >&2; exit 1; }
SDK_VERSION="0.2.178"          # pinned: the version the bridge was written and checked against (2026-11-06)
"$PY" -I -m venv --clear .claude-bridge/venv
.claude-bridge/venv/bin/python -I -m pip install --disable-pip-version-check --quiet "claude-agent-sdk==$SDK_VERSION"
.claude-bridge/venv/bin/python -I -c 'import claude_agent_sdk as s; print("claude-agent-sdk", s.__version__, "ready in .claude-bridge/venv")'

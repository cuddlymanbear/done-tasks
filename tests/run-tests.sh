#!/usr/bin/env bash
# Run both test suites:
#   * python  — the board/plug-in API tests (throwaway board, live DB never touched)
#   * node    — the UI harnesses against the real desktop/plugin.js
#
#   ./tests/run-tests.sh
#
# The python tests need a Hermes install (its `hermes_cli` package). `install.sh` copies
# this repo's core modules into it, so run install.sh first.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_AGENT_REPO="${HERMES_AGENT_REPO:-$HERMES_HOME/hermes-agent}"

PY="${HERMES_PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x "$HERMES_AGENT_REPO/venv/bin/python" ]; then PY="$HERMES_AGENT_REPO/venv/bin/python"; else PY="$(command -v python3)"; fi
fi

echo "== python tests (board + plug-in API) =="
# HERMES_KANBAN_* are injected into Hermes worker shells and would point the tests at a
# live board; drop them so the fixtures stay isolated (this is what bit us once).
env -u HERMES_KANBAN_DB -u HERMES_KANBAN_HOME -u HERMES_KANBAN_BOARD \
  PYTHONPATH="$HERMES_AGENT_REPO${PYTHONPATH:+:$PYTHONPATH}" \
  "$PY" -m pytest -q "$ROOT/tests/test_done_tasks_summary.py" "$ROOT/tests/test_kanban_done_tasks_archive.py"

echo
echo "== UI harnesses (real plugin.js) =="
cd "$ROOT/tests/ui"
node --import ./loader-hook.mjs harness.mjs
node --import ./loader-hook.mjs interaction.mjs
node --import ./loader-hook.mjs realbackend.mjs

echo
echo "All suites passed. (Optional: python3 tests/ui/render_check.py — needs playwright + chromium)"

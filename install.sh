#!/usr/bin/env bash
# Install the Done Tasks plug-in into a Hermes install.
#
#   ./install.sh                 # ~/.hermes + ~/.hermes/hermes-agent
#   HERMES_HOME=/path/to/.hermes HERMES_AGENT_REPO=/path/to/hermes-agent ./install.sh
#
# Idempotent: it copies (overwrites) the plug-in files and then tries to apply the one
# core patch that adds the board's digest table. Nothing is deleted.
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_AGENT_REPO="${HERMES_AGENT_REPO:-$HERMES_HOME/hermes-agent}"

say() { printf '  %s\n' "$*"; }

echo "Done Tasks plug-in -> install"
echo "  repo          : $SELF"
echo "  HERMES_HOME   : $HERMES_HOME"

# --- 1. python plug-in half (hook + HTTP API) -> <HERMES_HOME>/plugins/done-tasks/ ----
mkdir -p "$HERMES_HOME/plugins/done-tasks"
cp -f "$SELF/plugin/__init__.py"                "$HERMES_HOME/plugins/done-tasks/__init__.py"
cp -f "$SELF/plugin/plugin.yaml"                "$HERMES_HOME/plugins/done-tasks/plugin.yaml"
mkdir -p "$HERMES_HOME/plugins/done-tasks/dashboard/dist"
cp -f "$SELF/plugin/dashboard/manifest.json"    "$HERMES_HOME/plugins/done-tasks/dashboard/manifest.json"
cp -f "$SELF/plugin/dashboard/plugin_api.py"    "$HERMES_HOME/plugins/done-tasks/dashboard/plugin_api.py"
# The panel itself. Without dist/index.js the manifest advertises an entry that is not
# there and the tab renders empty — the copy is load-bearing, not optional.
cp -f "$SELF/plugin/dashboard/dist/index.js"    "$HERMES_HOME/plugins/done-tasks/dashboard/dist/index.js"
cp -f "$SELF/plugin/dashboard/dist/style.css"   "$HERMES_HOME/plugins/done-tasks/dashboard/dist/style.css"
say "plug-in half  -> $HERMES_HOME/plugins/done-tasks/ (incl. dashboard/dist)"

# The same tree belongs in the install's bundled plugins dir when that dir is the
# repo's plugins/ (a source install). Keep both in step so discovery finds one of them.
if [ -d "$HERMES_AGENT_REPO/plugins" ]; then
  mkdir -p "$HERMES_AGENT_REPO/plugins/done-tasks/dashboard/dist"
  cp -f "$SELF/plugin/__init__.py"              "$HERMES_AGENT_REPO/plugins/done-tasks/__init__.py"
  cp -f "$SELF/plugin/plugin.yaml"              "$HERMES_AGENT_REPO/plugins/done-tasks/plugin.yaml"
  cp -f "$SELF/plugin/dashboard/manifest.json"  "$HERMES_AGENT_REPO/plugins/done-tasks/dashboard/manifest.json"
  cp -f "$SELF/plugin/dashboard/plugin_api.py"  "$HERMES_AGENT_REPO/plugins/done-tasks/dashboard/plugin_api.py"
  cp -f "$SELF/plugin/dashboard/dist/index.js"  "$HERMES_AGENT_REPO/plugins/done-tasks/dashboard/dist/index.js"
  cp -f "$SELF/plugin/dashboard/dist/style.css" "$HERMES_AGENT_REPO/plugins/done-tasks/dashboard/dist/style.css"
  say "plug-in half  -> $HERMES_AGENT_REPO/plugins/done-tasks/ (bundled copy)"
fi

# --- 2. desktop panel -> <HERMES_HOME>/desktop-plugins/done-tasks/plugin.js -----------
mkdir -p "$HERMES_HOME/desktop-plugins/done-tasks"
cp -f "$SELF/desktop/plugin.js" "$HERMES_HOME/desktop-plugins/done-tasks/plugin.js"
say "desktop panel -> $HERMES_HOME/desktop-plugins/done-tasks/plugin.js"

# --- 3. core helper modules -> <hermes-agent>/hermes_cli/ -----------------------------
if [ -f "$HERMES_AGENT_REPO/hermes_cli/kanban_db.py" ]; then
  cp -f "$SELF/core/hermes_cli/done_tasks_summary.py" "$HERMES_AGENT_REPO/hermes_cli/done_tasks_summary.py"
  cp -f "$SELF/core/hermes_cli/done_tasks_archive.py" "$HERMES_AGENT_REPO/hermes_cli/done_tasks_archive.py"
  say "core modules  -> $HERMES_AGENT_REPO/hermes_cli/"

  # --- 4. the board's digest table (one patch, done-tasks only) ----------------------
  if grep -q "task_done_summaries" "$HERMES_AGENT_REPO/hermes_cli/kanban_db.py"; then
    say "core patch    -> already applied (task_done_summaries present)"
  elif git -C "$HERMES_AGENT_REPO" apply --check \
        "$SELF/core/patches/kanban_db-task_done_summaries.patch" 2>/dev/null; then
    git -C "$HERMES_AGENT_REPO" apply "$SELF/core/patches/kanban_db-task_done_summaries.patch"
    say "core patch    -> applied to hermes_cli/kanban_db.py"
  else
    say "core patch    -> COULD NOT APPLY (see core/patches/README.md, add the two blocks by hand)"
  fi
else
  say "core modules  -> SKIPPED: no hermes_cli/kanban_db.py under $HERMES_AGENT_REPO"
  say "                 set HERMES_AGENT_REPO if the Hermes install lives elsewhere"
fi

cat <<EOF

Next:
  1. enable the plug-in for the profile(s) that should use it:
       hermes plugins enable done-tasks
     (or add 'done-tasks' under 'plugins: enabled:' in that profile's config.yaml)
  2. restart the gateway / desktop app so the panel and the HTTP routes load:
       hermes gateway restart        # or: systemctl --user restart <gateway unit>
  3. backfill digests for cards that finished before today (optional):
       /done-summary backfill --limit 50
  4. open the board and look for "Done Tasks" in the sidebar.

Verify:  ./tests/run-tests.sh   (or see README "Running the tests")
EOF

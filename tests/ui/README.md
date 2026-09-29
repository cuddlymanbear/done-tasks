# UI harnesses

These load the **real** `desktop/plugin.js` (not a copy) with a stand-in for
`@hermes/plugin-sdk` and real React, so the plug-in's own query function, board fallback,
row rendering and archive payload all actually run.

```
node --import ./loader-hook.mjs harness.mjs      # 36 checks: live /done, board fallback,
                                                # loading/empty/error, archive payload, degraded write
node --import ./loader-hook.mjs interaction.mjs  # jsdom, 23 checks: clicking "Mark for archive"
                                                # removes the row; the Archived view labels its
                                                # action "Restore", shows a row archived in the
                                                # same page instance (D2) and lists archived
                                                # rows only with an "N archived" count (D3)
node --import ./loader-hook.mjs realbackend.mjs  # a /done-shaped payload straight into the panel
python3 render_check.py                          # Chromium 1280px + 1024px, no horizontal overflow
```

`render_check.py` needs playwright + chromium (`pip install playwright && playwright install
chromium`) and should be run after `harness.mjs`, which writes the markup it reads from
`tests/ui/out/`.

## Where react comes from

`paths.mjs` resolves react / react-dom / jsdom from the first of:

1. `$HERMES_AGENT_REPO`
2. this repo (`npm install` in the repo root)
3. `~/.hermes/hermes-agent`

Nothing machine-specific is hardcoded, so a fresh clone works once one of those exists.

## What the plugin claims, and what these prove

- `harness.mjs` feeds the spec's `/done` shape and asserts the badge, the amber accent
  rail, the fallback labelling, and that the archive POST carries `task_ids` **and** `actor`.
- `interaction.mjs` clicks the real button in a real DOM and asserts the row leaves the
  default view (and comes back through the Archived filter). It also pins the Archived
  view's own contract (card t_cbc4f9dc): the per-row action reads `Restore` with the
  restore tooltip and really calls `POST /unarchive`, the Archived list is archived-only
  with an `N archived` count, and a row archived in the same page instance is visible
  there without reopening the page.
- `realbackend.mjs` uses `fixtures/real-done.json`, a **shape-identical, anonymised** copy
  of a real backend payload: 12 items, 6 rows worth of titles, no real board content.
  `fixtures/live-board.json` is the same idea for the board-digest fallback.

The fixtures are anonymised on purpose — this repo is publishable. If you re-capture them
from a live board, scrub client names, ticket text and money figures first.

# Done Tasks — Hermes desktop panel

A readable digest of completed Kanban tasks, so the board owner can see what the fleet
actually got done instead of watching cards pile up unread.

`plugin.js` installs to `<hermes home>/desktop-plugins/done-tasks/plugin.js` — the runtime
disk-plugin door the desktop app scans. Plain ESM, **no build step**; it imports only
`@hermes/plugin-sdk`, `react` and `react/jsx-runtime`, which is the loader's full allowlist.

## What it gives you

A full page at `/done-tasks` plus a sidebar nav row ("Done Tasks").

- Completed tasks, **newest first**, each row showing the task title, the assignee bot, when
  it finished (relative, exact timestamp on hover), the generated plain-words summary, and
  an artifact count when the task produced files.
- A **REVIEW badge** on any row the fleet flagged; hovering (or focusing) it shows the
  reason and the fired rule ids, e.g. `Touches money or pricing. · Rules: R2, R7`.
- Review-flagged rows are visually prioritised: an amber accent rail, a tinted surface and
  the badge. The rail is a shape cue, not just colour, so it survives a bad laptop panel or
  a projector.
- **Mark for archive** on every row. Archived rows leave the default view.
- A three-way view switch: **All done** / **Needs review** / **Archived**. The Archived view
  lists what you filed and offers the restore path.
- Loading skeletons, an empty state, and an error state that says what failed.

## API it talks to

Everything goes through `ctx.rest`, which by construction is scoped to
`/api/plugins/done-tasks`, using the frozen shapes in `../docs/DONE-TASKS-PLUGIN-SPEC.md` §5:

| Call | Body | Purpose |
|---|---|---|
| `GET /done` | `?board=&limit=&offset=&include_archived=&only_review=` | the list |
| `GET /done/{task_id}` | — | one task's detail |
| `POST /archive` | `{ task_ids: [...], actor: "<who>" }` | mark for archive |
| `POST /unarchive` | `{ task_ids: [...], actor: "<who>" }` | restore |

`actor` is always sent (the spec makes it required and never defaulted). It comes from
`ACTOR` at the top of `plugin.js` — change that one constant to change who the archive
records are attributed to.

## Degradation — read this before wondering why the badge is missing

The routes are mounted by the plug-in's python half (`../plugin/dashboard/plugin_api.py`).
If that half is not installed (or the gateway has not been restarted since it was), the
page degrades in two documented steps instead of showing an empty box:

1. **Primary** — `ctx.rest('/done')`. Used as soon as the endpoint is mounted. Nothing else
   in the panel changes when that happens.
2. **Fallback** — a direct read of the already-live `/api/plugins/kanban/board` endpoint,
   synthesising rows from its `done` column. Rows carry `summary_source: 'board_fallback'`,
   the page shows an amber banner naming the gap, and each row is labelled
   "Board digest only".

While in fallback mode the **archive buttons are disabled** with an explanatory tooltip,
rather than pretending a write succeeded.

## Verifying it

See `../tests/ui/README.md`. In short, from `tests/ui/`:

```
node --import ./loader-hook.mjs harness.mjs
node --import ./loader-hook.mjs interaction.mjs
node --import ./loader-hook.mjs realbackend.mjs
python3 render_check.py
```

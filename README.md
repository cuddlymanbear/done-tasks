# Done Tasks

A Hermes plug-in that turns "completed Kanban tasks" into something a human can actually
read: one short plain-words summary per finished card, a deterministic flag on the ones
that probably want your eyes, and a one-tap archive so the finished pile stops growing.

Built for the Osoyoos Signs bot fleet (Hermes Agent), 2026-09-28.

---

## What it does

**On the board page.** A "Done Tasks" view lists finished cards newest-first. Each row
shows the title, which bot did it, when it finished, the generated summary, and an artifact
count. Rows the fleet flagged carry a REVIEW badge whose tooltip names the reason and the
rule that fired. A three-way switch gives you *All done* / *Needs review* / *Archived*, and
"Mark for archive" takes a reviewed row out of the default list (reversible from the
Archived view).

**Under the hood.** Every time a task completes, a lifecycle hook writes one row into
`task_done_summaries` in the board's database: the summary, where the summary came from, the
task's artifacts, and the review verdict. A deterministic rule table (R1–R9) decides the
review flag — things like "touched money", "changed live config", "produced no artifacts" —
so the flag is testable rather than a vibe. If the summariser is unavailable the row still
gets written with a title/body fallback, so the list never renders blank.

**Archive is soft and separate.** `archive_state` is the plug-in's own field on its own
table. It never writes `tasks.status = 'archived'`, which stays the board's own terminal
state.

---

## Layout — and where each part installs

This repo is a flat mirror of the two Hermes trees it lands in.

| In this repo | Installs to | What it is |
|---|---|---|
| `plugin/` (`__init__.py`, `plugin.yaml`) | `<hermes home>/plugins/done-tasks/` | the python half: the `kanban_task_completed` hook and the `/done-summary` command |
| `plugin/dashboard/` (`manifest.json`, `plugin_api.py`) | same, `.../done-tasks/dashboard/` | the HTTP routes mounted at `/api/plugins/done-tasks/` |
| `desktop/plugin.js` (+ `desktop/README.md`) | `<hermes home>/desktop-plugins/done-tasks/` | the panel the desktop app renders (plain ESM, no build) |
| `core/hermes_cli/done_tasks_*.py` | `<hermes-agent repo>/hermes_cli/` | the summary/rule/archive logic, importable and testable without a plug-in |
| `core/patches/kanban_db-task_done_summaries.patch` | applied to `<hermes-agent repo>` | the one core change: the digest table (see `core/patches/README.md`) |
| `tests/` | nothing | python API tests + the node UI harnesses |
| `docs/DONE-TASKS-PLUGIN-SPEC.md` | nothing | the frozen build spec (data model, rules, API shapes) |

## Install

```
git clone <this repo> done-tasks
cd done-tasks
./install.sh                       # ~/.hermes + ~/.hermes/hermes-agent
```

Other locations:

```
HERMES_HOME=/path/to/.hermes HERMES_AGENT_REPO=/path/to/hermes-agent ./install.sh
```

Then:

1. **Enable it** for the profile that should use it — `hermes plugins enable done-tasks`, or
   put `done-tasks` under `plugins: enabled:` in that profile's `config.yaml`.
2. **Restart the gateway / desktop app** so the panel and the HTTP routes load.
3. Optional: backfill digests for cards that finished before it was installed —
   `/done-summary backfill --limit 50` (needs no cron; the completion hook is live from then on).

### Requirements and environment

- A Hermes install with the Kanban plug-in (the digest table lives in the board DB).
- **No new environment variables and no secrets.** The plug-in reads no credentials of its
  own; summaries are produced through the install's own auxiliary-LLM helper
  (`agent.auxiliary_client.call_llm`), using whatever model that install is configured with.
  Two variables only tell `install.sh` *where* things are: `HERMES_HOME`,
  `HERMES_AGENT_REPO`. The tests read the same two.
- If the model is unavailable, nothing breaks: rows fall back to the title and are marked
  `fallback_title_body` / `pending`, and the sweep (`/done-summary backfill`) retries them.

## Running the tests

```
./tests/run-tests.sh
```

- **python** — `tests/test_done_tasks_summary.py` (rule table R1–R9, each rule with a positive
  and a negative case, plus the read API shape) and `tests/test_kanban_done_tasks_archive.py`
  (mark-for-archive → leaves the default listing; un-archive → comes back; still fetchable by
  id). Both run against a throwaway board in a temp dir and strip `HERMES_KANBAN_*` so they can
  never touch a live board.
- **node** — `tests/ui/` loads the real `desktop/plugin.js` with real React and asserts the
  rendering, the archive payload contract, the degraded-write path, and (in jsdom) that
  clicking archive removes the row. `tests/ui/README.md` has the details, including
  `render_check.py` for the no-horizontal-overflow check at laptop widths.

Node tests need react / react-dom / jsdom: `npm install` here, or point
`HERMES_AGENT_REPO` at a Hermes install that has them.

## Status and known limits

- The python half, the routes and the panel are all in place; **the routes only mount once
  the plug-in is installed and the gateway restarted** — until then the panel runs in its
  documented board-digest fallback (amber banner, archive buttons disabled).
- The core patch is against Hermes commit `6ab8f80d6825951759cc24c6b5699060b26e17a1`. A
  Hermes update may move the lines it touches; `core/patches/README.md` gives the same two
  edits by hand.
- The UI fixtures are **anonymised** copies of real payload shapes, not real board content.
  Re-capturing them from a live board means scrubbing client names, ticket text and money
  figures first.

## Provenance

Built from the board cards `t_1d19e4b5` (the ask), `t_ace3c9aa` (spec, `docs/`),
`t_94fd868b` (summary + review flag), `t_cfcf0794` (archive + HTTP surface) and
`t_5f4fcaf7` (this panel), plus the test harnesses those cards produced.

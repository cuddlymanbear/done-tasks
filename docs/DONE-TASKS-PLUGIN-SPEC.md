# Done Tasks Summary plug-in — build spec (v1)

Task: t_ace3c9aa · Author: gnosis · Status: implementable, no open design decisions
Repo of record: `/home/hermes/.hermes/hermes-agent` @ `6ab8f80d68`

This spec is grounded in the shipped code. Every path, column and function named below was
read before writing. Backend tasks (`t_94fd868b` generate+flag, `t_cfcf0794` archive) and the
UI task (`t_5f4fcaf7`) can implement directly from §1–§6 without further design decisions.

---

## 0. What already exists (do not re-invent it)

| Need | Already shipped | Where |
|---|---|---|
| Completion event + hook | `_append_event(..., "completed", payload)`; lifecycle hook `kanban_task_completed` fires AFTER commit | `hermes_cli/kanban_db.py:2814` (event), `:2826` (hook) |
| Worker handoff summary | `task_runs.summary` (written by `complete_task`) | `hermes_cli/kanban_db.py:2798` |
| Newest summary per task, 1 query | `kanban_db.latest_summary()` / `latest_summaries()` | `kanban_db.py:4435`, `:4446` |
| Artifact paths at completion | `metadata["artifacts"]` promoted onto the completed event payload | `kanban_db.py:2921` `_completed_event_payload` |
| Deduped event payload for notifiers | `payload.completed.summary` = first line, 400 chars | same fn, `:2935` |
| Board list with done column, newest-first | `GET /api/plugins/kanban/board` → `columns[].tasks[]` | `plugins/kanban/dashboard/plugin_api.py:279` `get_board` |
| "hide archived from the default view" | `list_tasks(include_archived=False)` already excludes `status='archived'` | `kanban_db.py:1532` |
| Hard archive (terminal, reversible-in-principle) | `kanban_db.archive_task()` → `status='archived'`, emits `archived` event | `kanban_db.py:3882` |
| Dashboard archive action | `PATCH /tasks/{id}` with `{"status":"archived"}` → `_apply_status` → `archive_task` | `plugin_api.py:668`, `:582` |
| Hook surface for a plugin to observe completion | `invoke_hook("kanban_task_completed", task_id, board, assignee, run_id, profile_name, summary)` | `hermes_cli/plugins.py:162`, `hermes_cli/lifecycle.py` |
| LLM side-task helper used by sibling features | `agent.auxiliary_client.call_llm(task=..., messages=..., temperature=0.0, max_tokens=..., timeout=...)` + tolerant JSON-blob extract | `plugin_api.py:1064` `_run_estimate` |
| Desktop plug-in layout | `~/.hermes/plugins/<id>/dashboard/{manifest.json,plugin_api.py}` mounts at `/api/plugins/<id>/` | `website/docs/developer-guide/desktop-plugin-sdk.md:1336` |

**Grounded facts from the live board** (`/home/hermes/.hermes/kanban.db`, read 2026-09-28):

- 78 `done`, 41 `archived`, 16 `ready`, 29 `todo`, 8 `running`, 1 `blocked`.
- **`tasks.result` is NULL on every recent done task** (10/10 sampled). The human-readable
  text lives in `task_runs.summary`. **Any implementation that reads `tasks.result` for the
  summary will render blank cards.** This is the single most important fact in this spec.
- 138 rows in `task_attachments`; `completed` event payloads carry `artifacts` only when the
  worker passed `metadata["artifacts"]` (2 of 3 sampled had it; the third had none — so the
  "no artifacts" review rule in §3 has real positive cases today).
- `metadata` keys seen in the wild: `artifacts`, `changed_files`, `decisions`, `findings`,
  `follow_up`, `worker_session_id`, `cards_created`, `not_touched`. Artifacts sometimes live
  under `metadata["artifacts"]` as a **stringified Python list**, not a JSON list — the
  extractor in §2.3 must tolerate that.

---

## 1. Data model

### 1.1 Decision

**Do not add columns to `tasks`.** Add one new table, `task_done_summaries`, keyed 1:1 on
`task_id`, in the same DB as the board it describes. Rationale: the board is already reading
`tasks`/`task_runs` on every render; a side table keeps the summary/flag/archive state
additive, migration-cheap, and immune to `_rebuild_drifted_tables` semantics. Board DB path
resolution stays as-is (`kanban_db.kanban_db_path(board)`, `kanban_db.py:507`).

Note the naming collision to avoid: the word **`archived` already means the terminal
`tasks.status`** (`VALID_STATUSES`, `kanban_db.py:103`). The plug-in's archive is a *separate,
soft* concept and must never be spelled `status='archived'` in new code. `archive_state`
below is the plug-in's own field.

### 1.2 DDL (append to `SCHEMA_SQL` in `hermes_cli/kanban_db.py:868`)

```sql
-- Done Tasks plug-in: per-completed-task digest. One row per done task, written by the
-- summary pass; rows are never deleted by this feature (archive is a flag, not a delete).
CREATE TABLE IF NOT EXISTS task_done_summaries (
    task_id         TEXT PRIMARY KEY,
    -- Denormalised at generation time so a listing renders in ONE query and a later
    -- title/assignee edit does not silently rewrite history.
    title           TEXT NOT NULL,
    assignee        TEXT,
    completed_at    INTEGER NOT NULL,
    -- LLM pass (or fallback, see §2.3). 1-3 sentences, plain words, <= 600 chars.
    summary         TEXT NOT NULL,
    summary_source  TEXT NOT NULL DEFAULT 'llm',
    -- summary_source: llm | llm_repaired | fallback_title_body | manual
    -- JSON list of absolute paths / URLs the task produced (may be []).
    artifacts       TEXT NOT NULL DEFAULT '[]',
    artifact_count  INTEGER NOT NULL DEFAULT 0,
    review_flag     INTEGER NOT NULL DEFAULT 0,
    -- JSON list of fired rule ids, machine-readable, e.g. ["R2","R5"].
    review_reasons  TEXT NOT NULL DEFAULT '[]',
    -- Human-readable one-liner for the badge tooltip, <= 200 chars.
    review_reason   TEXT,
    review_rule_version TEXT NOT NULL DEFAULT 'v1',
    archive_state   TEXT NOT NULL DEFAULT 'active',
    -- archive_state: active | archive_requested | archived
    archive_requested_at INTEGER,
    archive_requested_by TEXT,
    archived_at     INTEGER,
    archived_by     TEXT,
    generated_at    INTEGER NOT NULL,
    generated_by    TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 1,
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_done_sum_completed ON task_done_summaries(completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_done_sum_state     ON task_done_summaries(archive_state, review_flag);
```

Migration: `hermes_cli/kanban_db_connect.py` runs `conn.executescript(SCHEMA_SQL)` then
`_migrate_add_optional_columns(conn)` on every `connect()` (`kanban_db_connect.py:726-732`),
so a plain `CREATE TABLE IF NOT EXISTS` in `SCHEMA_SQL` is sufficient for existing boards —
no entry in `_BASE_TASK_COLUMNS` / `_LATER_TASK_COLUMNS` / `_REBUILD_SPECS` is needed
(those exist only for *column drift on existing tables*). Because `SCHEMA_SQL` changes, the
guard test `test_rebuilt_schema_matches_fresh` (referenced at `kanban_db_connect.py:1004`)
must still pass — a fresh DB and a rebuilt legacy DB must both end up with the new table.

`task_id` references `tasks.id` but carries **no FK clause** (the schema has none anywhere,
and `delete_task` deletes relations explicitly — `kanban_db.py:3929` `_delete_task_relations`,
whose table list is literally `("task_comments", "task_events", "task_runs",
"kanban_notify_subs")`). Add `task_done_summaries` to that tuple so a hard delete stays clean.
`delete_archived_task` stays restricted to `status='archived'`; the plug-in never hard-deletes.

### 1.3 Field provenance (who writes what)

| Field | Source |
|---|---|
| `task_id`, `title`, `assignee`, `completed_at` | copied from `tasks` at generation |
| `summary` | LLM pass, or fallback (§2.3) |
| `summary_source` | the generator |
| `artifacts`, `artifact_count` | extracted from `task_runs.metadata.artifacts` **and** the `completed` event payload's `artifacts` **and** rows in `task_attachments` (union, deduped — §2.4) |
| `review_flag`, `review_reasons`, `review_reason`, `review_rule_version` | deterministic rule engine (§3). The LLM may **add** reasons via rule R8 but may never clear a deterministic hit |
| `archive_state` + 4 archive fields | the archive endpoint (§4) |
| `generated_at`, `generated_by`, `attempt_count`, `last_error` | the generator |

---

## 2. Where and how the summary is generated

### 2.1 Trigger: BOTH, with the hook as the primary path

1. **Primary — at completion.** Subscribe to `kanban_task_completed`
   (`hermes_cli/plugins.py:162`; fires after commit with `task_id, board, assignee, run_id,
   profile_name, summary`). Generate the record there so the card is never briefly summary-less.
   The hook is best-effort and swallowed on error (`kanban_db.py:150-159`), so it must never
   raise and must never be the only path.
2. **Sweep — backfill and repair.** A scheduled sweep (gateway watcher, 60 s cadence, same
   shape as the existing kanban watchers in `gateway/kanban_watchers*.py`) selects done tasks
   with no row, or with `summary_source='fallback_title_body'`, or `attempt_count < 3 AND
   last_error IS NOT NULL`, and generates/regenerates. This is what covers tasks that were
   already done before the plug-in shipped (78 exist today) and completions from any process
   that does not load plug-ins.

Selection query (parameterised, bounded):

```sql
SELECT t.id, t.title, t.body, t.assignee, t.completed_at, t.created_by, t.workspace_path
  FROM tasks t
  LEFT JOIN task_done_summaries s ON s.task_id = t.id
 WHERE t.status = 'done' AND t.completed_at IS NOT NULL
   AND (s.task_id IS NULL
        OR s.summary_source = 'fallback_title_body'
        OR (s.last_error IS NOT NULL AND s.attempt_count < 3))
 ORDER BY t.completed_at DESC
 LIMIT 25;
```

### 2.2 Every done task gets exactly one row

Idempotent `INSERT ... ON CONFLICT(task_id) DO UPDATE`, never `INSERT` alone. The sweep and
the hook may race; the second writer wins and bumps `attempt_count`. Never delete a row here —
`archive_state` is the only way a record leaves the default view.

### 2.3 LLM pass — shape, and its failure modes

Model: **not pinned.** Use the shipped side-task router, exactly as the estimate endpoint
does (`plugin_api.py:1064` `_run_estimate`): `from agent.auxiliary_client import call_llm`,
then `call_llm(task="kanban_done_summary", messages=[...], temperature=0.0, max_tokens=320,
timeout=45)` with the headless affinity scope (`set_affinity_scope`) — the comment at
`plugin_api.py:1085` records why: without a bound scope the relay answers `400
MissingSessionID`. This routes main provider → OpenRouter → Nous → … automatically and needs
no new config key.

Prompt shape (fixed, versioned as `review_rule_version`'s sibling `prompt_version='v1'`):

- system: *"You summarise one completed software/ops task for a non-technical shop owner.
  Reply with JSON only: {\"summary\": string, \"review_hint\": string|null}. `summary` is
  1–3 short sentences, plain words, no file paths, no ids, no tool names, no markdown, max
  600 characters. Say what was actually done and what it means. `review_hint` is null unless
  the owner should personally look at this; if so, one short sentence saying why."*
- user: `Title`, `Body` (cap 4000 chars), `Assignee`, `Final handoff summary` (the
  `task_runs.summary`, cap 4000), `Artifact count`, `Runs` (count + any non-`completed`
  outcomes). No raw worker log, no comment thread (cost + privacy).

Deterministic JSON extraction: reuse the tolerant blob extraction pattern from
`_run_estimate` (`plugin_api.py:1099-1103` — `json.loads` of the raw text, else
`re.search(r"\{.*\}", raw, re.DOTALL)`), which is the same shape `specify`/`decompose` use.

Failure ladder — each step is a real, testable state, never a crash:

| Case | Result |
|---|---|
| `call_llm` raises (no key, 402, timeout) | `summary` = fallback, `summary_source='fallback_title_body'`, `last_error='<ExceptionType>'`, attempt++ → sweep retries |
| Model returns unparseable text | same as above, `last_error='parse_failed'` |
| Model returns valid JSON but `summary` empty/short (<15 chars) | same as above, `last_error='empty_summary'` |
| Model returns valid JSON, summary fine | `summary_source='llm'`, `last_error=NULL` |
| Sweep has retried 3× | row stays on `fallback_title_body`; it is still listed and still readable |

**Fallback summary text (must be usable, never blank):**
`_first_line(title, 200)` + `" "` + `"[AI summary unavailable — "` + why + `"]"`.
The degradation is visible in the UI as a plain-text badge (§5.4), not hidden.

### 2.4 Artifact extraction (deterministic, no LLM)

Union of three sources, then dedupe by absolute path/URL:
1. `task_runs.metadata["artifacts"]` — read the **latest completed run** first, then `tasks.result`-era rows.
2. the `completed` event payload's `artifacts` (`_append_event(..., "completed", ...)`,
   promoted by `_completed_event_payload`, `kanban_db.py:2921`).
3. `task_attachments.stored_path` for the task (`kanban_db.py:1894` `list_attachments`).

The extractor must accept a JSON list, a JSON string containing a list, **and** a
stringified Python list (`"['/a/b', '/c/d']"` — observed live in `metadata.artifacts`).
Use `ast.literal_eval` as the second attempt after `json.loads`, then fall back to
splitting on newlines. Drop blanks; keep at most 25; `artifact_count` is the post-dedupe len.
Never let a malformed blob raise — a task is never unsavable because it declared artifacts badly.

---

## 3. Review-flag heuristic (deterministic, unit-testable)

`review_flag` is computed by a pure function with no I/O:

```python
def evaluate_review_rules(ctx: DoneTaskContext) -> tuple[bool, list[str], str | None]
```

`DoneTaskContext` carries: `title, body, created_by, assignee, artifacts (list),
runs (list of {status, outcome, attempt_index}), events (list of {kind}),
metadata (dict), session_id, changed_files (list)`.

Each rule is a pure predicate over that context. `review_flag = any(rule fired)`.
`review_reasons` = fired rule ids in ascending order. `review_reason` = the first fired
rule's human sentence (rule order = priority order).

| id | Fires when (testable condition) | Badge sentence |
|---|---|---|
| **R1** | `completed_at - started_at > 3600` **or** the task is a parent whose `children` count ≥ 3 in `task_links`. *Changed something big.* | "Large job — a lot changed at once." |
| **R2** | The union of `body + title + summary` matches the money regex `(?i)\b(money\|invoice\|invoic\|payment\|refund\|credit\|charge\|price\|pricing\|quote\|quote#\|estimate_amount\|\\$\\s?\\d\|purchase\|billing\|payable\|receivable\|deposit)\b`. *Touched money.* | "Touches money or pricing." |
| **R3** | The union of `body + title + summary + changed_files + artifacts` matches any of: `\.env`, `config\.yaml`, `credentials`, `secret`, `password`, `token`, `api[_-]?key`, `systemd/`, `/etc/`, `crontab`, `authorized_keys`, `sudoers`. *Changed live config or secrets.* | "Changed live configuration or credential material." |
| **R4** | `artifact_count == 0` **and** no `task_attachments` row **and** the task was not created by `auto-decomposer`-style bookkeeping whose body matched `(?i)\b(self-test\|no build\|nothing to build)\b`. *Produced nothing.* | "Finished without producing a file or attachment." |
| **R5** | Any of: `attempt_index > 1`, `consecutive_failures > 0`, `runs` contains a run with `status in {crashed, timed_out, failed, reclaimed}` or `outcome in {crashed, timed_out, gave_up, spawn_failed, reclaimed}`, or `task_events` for the task contains a `kind='archived'` followed by a later `kind='completed'` (archived-then-reopened). *Was re-run.* | "Had to be retried or restarted before it finished." |
| **R6** | The union of `body + title + summary` matches the destructive regex `(?i)\b(delete\|deleted\|drop\|dropped\|truncate\|purge\|wipe\|rm -rf\|overwrite\|overwrote\|migrat\|migrated\|reset\|rollback\|rotat\|rotate)\b`. *Said destructive things.* | "Talks about deleting, overwriting or rotating something." |
| **R7** | `changed_files` (metadata or the union of artifact paths) contains a path inside another profile's tree: `re.match(r"^/home/[^/]+/\.hermes/profiles/([^/]+)/", path)` and that profile `!= assignee`. *Edited shared or someone else's state.* | "Changed files belonging to another bot." |
| **R8** | The LLM returned a non-null, non-empty `review_hint`. *Advisory only — can ADD a flag, never remove one.* | the model's own sentence, redacted and truncated to 200 chars |
| **R9** | `session_id` is not null (the card was created from inside an agent loop) **and** an artifact path lies outside `kanban_db.workspaces_root(board)`. *Work landed outside the sandbox.* | "Wrote outside the normal workspace." |

Rules that must **never** fire, and are negative test cases: a plain read-only investigation
that produced a markdown report, a self-test card whose body says nothing was to be built, and
a summary card with no artifacts but which is `created_by='auto-decomposer'` bookkeeping.

Safety properties the implementation must hold, each with its own test:

- `review_flag` is **monotonic within a generation attempt**: re-running the rule engine over
  the same context yields the identical `(flag, reasons)` tuple. Same input → same output.
- A rule id in `review_reasons` always implies `review_flag == 1`; `review_flag == 1` always
  implies `review_reasons != []`. Neither can be set without the other.
- R8 cannot clear a deterministic hit: a context that fires R2 and where the model returns
  `review_hint: null` still has `review_flag == 1` with `reasons == ["R2"]`.
- Every rule has ≥1 positive and ≥1 negative unit case. Rule ids are frozen strings; adding a
  rule bumps `review_rule_version` to `v2` and requires updating the test table.
- Regeneration after a rules-version bump must be possible without losing `archive_state`.

---

## 4. Archive semantics

**Soft, reversible, never a delete.**

- `archive_state` values and the only legal transitions:
  - `active` → `archive_requested`: owner taps **Mark for archive**.
  - `archive_requested` → `archived`: the sweep promotes it (default: immediately, same write;
    the intermediate state exists so a future "undo for 10 s" or a batch confirm needs no
    schema change, and so an `archive_requested` row is never lost if the promotion write
    fails).
  - `archive_requested` **or** `archived` → `active`: **Unarchive**. This is the reversal path
    and it must clear `archived_at`, `archived_by`, `archive_requested_at`,
    `archive_requested_by` (all four) so a re-archive is clean and the timestamps never lie.
- **Who:** any caller the dashboard auth already admits — the plug-in adds no second auth
  system. `archived_by` / `archive_requested_by` record the acting identifier the request
  carries (dashboard session identity), never a guessed default.
- **Effect:** exactly one thing — the record is excluded from the default Done listing
  (§5.2 `GET /done` without `include_archived`). It is still returned by `/done?include_archived=true`,
  still returned by `GET /done/{task_id}`, and the underlying `tasks` row, its runs, events,
  comments and attachments are untouched.
- **Never:** call `kanban_db.archive_task()` from this feature. That sets the terminal
  `tasks.status='archived'` (`kanban_db.py:3882`), kills a running worker and reaps the
  workspace — it would make the card vanish from the board entirely and is not what the owner
  asked for when they say "file this". Equally never call `delete_task` /
  `delete_archived_task`. The plug-in's archive is orthogonal to task status; a task may be
  `status='done'` and `archive_state='archived'` forever, and that is the normal end state.
- The two archives may be reconciled later by whoever wants it: if `tasks.status='archived'`,
  the done listing already drops the card anyway, and the sweep must **not** then write an
  `archive_state` change — it leaves the summary row as it is.

---

## 5. API surface

New desktop plug-in, id `done-tasks`, layout per
`website/docs/developer-guide/desktop-plugin-sdk.md:1336`:
`~/.hermes/plugins/done-tasks/dashboard/{manifest.json, plugin_api.py}` →
all routes mount under **`/api/plugins/done-tasks/`**. Same board query-param convention as
the kanban plug-in (`_BOARD_Q`, `plugin_api.py:44`; `_resolve_board`, `:65` — malformed slug
→ 400, unknown board → 404, omitted → active board).

Pydantic bodies follow the existing style (`plugin_api.py:413` `CreateTaskBody`).

### 5.1 `GET /done`

```
GET /api/plugins/done-tasks/done?board=<slug>&limit=50&offset=0
    &include_archived=false&only_review=false
```

Response `200`:

```json
{
  "board": "default",
  "now": 1790633500,
  "total": 78,
  "returned": 3,
  "items": [
    {
      "task_id": "t_1655b992",
      "title": "Stop the quoting bot posting the same quote twice",
      "assignee": "daemon",
      "created_by": "auto-decomposer",
      "completed_at": 1790633416,
      "summary": "The quoting bot can no longer post the same quote twice for one ticket. A guard now checks the ticket before a second price is written.",
      "summary_source": "llm",
      "artifacts": ["/home/hermes/.hermes/profiles/daemon/skills/quotes/dedupe.md"],
      "artifact_count": 1,
      "review_flag": true,
      "review_reasons": ["R2", "R7"],
      "review_reason": "Touches money or pricing.",
      "review_rule_version": "v1",
      "archive_state": "active",
      "archived_at": null,
      "archived_by": null,
      "generated_at": 1790633420,
      "fallback": false
    }
  ]
}
```

Ordering: `completed_at DESC` (matches the existing done-column sort, `plugin_api.py:329-331`).
Default `include_archived=false` → `WHERE s.archive_state != 'archived'`. `only_review=true`
adds `AND s.review_flag = 1`. Done tasks with **no** summary row yet are still returned, with
`summary_source='pending'` and `fallback=true` — the UI must never hide a card just because the
sweep hasn't reached it.

### 5.2 `GET /done/{task_id}`

Same object as an `items[]` entry, plus `body`, `result`, `runs[]`
(`{id, status, outcome, summary, ended_at}`), `events_tail[]` (`{kind, created_at}`),
`attachments[]` (`{id, filename, size}`), `children[]` / `parents[]`
(`{id, title, status}` — reuse `_links_for`/`_link_tasks`, `plugin_api.py:258`, `:265`), and
`prompt_version`. `404` with `{"detail": "task <id> not found"}` when no summary row and no
done task exists. Archived records are returned here regardless of state.

### 5.3 `POST /archive` and `POST /unarchive`

```
POST /api/plugins/done-tasks/archive?board=<slug>
{ "task_ids": ["t_1655b992", "t_abc123"], "actor": "chad.d@osoyoossigns.ca" }
```
```json
{ "ok": true, "archived": ["t_1655b992"],
  "already": [], "unknown": ["t_abc123"],
  "skipped": [{"task_id": "t_x", "reason": "task is not done"}] }
```
```json
{ "ok": false, "reason": "no task_ids supplied" }   // HTTP 400
```

- `task_ids`: 1–100 entries. Empty/missing → `400`. Batched so the UI can archive a
  multi-selection in one call (the existing UI already supports multi-select moves).
- `actor`: required, non-blank; trimmed to 200 chars; stored in `archived_by`. Missing →
  `400` (`{"ok": false, "reason": "actor is required"}`). Never defaulted.
- Idempotent: archiving an already-archived id returns it in `already`, `ok` stays `true`.
- Unknown id → `unknown`, never a 404 for the whole call.
- Only `status='done'` tasks may be archived; anything else → `skipped` with a reason.
  (Rationale: the board owner's request is about the Done lane.)
- `unarchive` is the exact mirror: sets `archive_state='active'` and clears all four archive
  fields; returns `{ok, unarchived, already, unknown, skipped}`.

### 5.4 `POST /regenerate/{task_id}`

```json
{ "ok": true, "task_id": "t_1655b992", "summary_source": "llm",
  "review_flag": true, "review_reasons": ["R2"] }
```

Re-runs the summary pass for one task, preserving `archive_state` and any manual edits
(`summary_source='manual'` is never overwritten by a sweep, only by an explicit call with
`{"force": true}`). Failure returns `{"ok": false, "reason": "...", "summary_source":
"fallback_title_body"}` with HTTP `200` — the estimate endpoint sets this
never-raise precedent (`plugin_api.py:1064`).

### 5.5 Python-side helpers (importable, for the backend task)

```python
# hermes_cli/done_tasks_summary.py  (new module; no core-file edits)
def generate_done_summary(conn, task_id, *, board=None, force=False) -> dict
def generate_pending(conn, *, board=None, limit=25) -> dict          # the sweep body
def evaluate_review_rules(ctx: DoneTaskContext) -> tuple[bool, list[str], str | None]
def extract_artifacts(conn, task_id) -> list[str]
def list_done(*, board=None, include_archived=False, only_review=False,
              limit=50, offset=0) -> dict
def set_archive_state(conn, task_ids, state, *, actor) -> dict
```

`generate_done_summary` must be safe to call from inside the `kanban_task_completed` hook
(it runs after the DB commit, in whatever process completed the task) and must swallow every
exception into `last_error` rather than raising into the hook. The hook is delivered by
`hermes_cli.lifecycle.invoke_hook` (`lifecycle.py:26`), which calls first-party observers
then plugin hooks; the plug-in registers a plugin hook, so a raising callback would be
isolated by `invoke_hook` anyway — belt and braces.

---

## 6. Acceptance checklist (what a reviewer should be able to point at)

1. Spec names the real files/functions it extends, and every named symbol exists at the
   cited location in `6ab8f80d68`. *(Verified by reading each one before writing it.)*
2. §1 DDL is a single additive `CREATE TABLE IF NOT EXISTS` in `SCHEMA_SQL`; no column is
   added to `tasks` or `task_runs`; `test_rebuilt_schema_matches_fresh` still passes.
3. §2.1 covers both a pre-existing done task (78 on the live board) and a completion from any
   profile, not only `default`.
4. §2.3 has a named degradation for each of: raise, parse failure, empty summary, exhausted
   retries — and none of them yields a blank card.
5. §3 lists 9 rules (R1–R9), each a pure predicate, each with a positive and negative case,
   plus four stated safety invariants.
6. §4 specifies soft-only archive, the four fields cleared on unarchive, and an explicit
   prohibition on `archive_task` / `delete_task`.
7. §5 enumerates 4 endpoints with method, path, params, request body and example response
   payload, plus 6 importable function signatures.
8. No code implemented in this task; the deliverable is the document.

## 7. Deliberate non-goals (so no one re-opens them as gaps)

- **No per-task email/Telegram push.** The gateway notifier already delivers completions
  (`kanban_notify_subs`, `gateway/kanban_watchers_notifier.py`); a digest channel is a
  separate decision for the owner, not part of this plug-in.
- **No new model/provider config key.** The auxiliary router's default chain is used.
- **No writes to `tasks.status`.** The board's own Archive verb keeps meaning what it means.
- **No LLM in the review flag.** R8 is advisory-add-only; every actionable rule is deterministic.


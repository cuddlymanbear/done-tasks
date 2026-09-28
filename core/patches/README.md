# The one core change this plug-in needs

`kanban_db-task_done_summaries.patch` adds the table the whole feature stores into
(`task_done_summaries`) plus its two indexes, and teaches the existing hard-delete helper
to clean that table up too. It is written against the Hermes tree at commit
`6ab8f80d6825951759cc24c6b5699060b26e17a1` and touches nothing else in the file.

Apply from the Hermes install root:

```
cd ~/.hermes/hermes-agent
git apply /path/to/done-tasks/core/patches/kanban_db-task_done_summaries.patch
```

`install.sh` does this for you when `HERMES_AGENT_REPO` points at the install.

## If the patch does not apply

A Hermes update may have moved the surrounding lines. The change is two small edits to
`hermes_cli/kanban_db.py`; make them by hand:

1. **Append the `CREATE TABLE`/index block to `SCHEMA_SQL`.** Find the end of the
   `kanban_notify_subs` table definition and insert, immediately before the
   `CREATE INDEX IF NOT EXISTS idx_tasks_status` line, the block that starts with
   `-- Done Tasks plug-in: per-completed-task digest` and ends with the two
   `idx_done_sum_*` index lines (copy them straight out of the patch file — the `+`
   lines are the content).

2. **Include the new table in the hard-delete sweep.** In `_delete_task_relations`,
   add `"task_done_summaries"` to the tuple of tables deleted by `task_id`.

Nothing else is required: `kanban_db.init_db()` creates the table from `SCHEMA_SQL` on the
next start, so an existing board picks it up without a migration.

## Checking it worked

```
python3 -c "from hermes_cli import kanban_db as kb; kb.init_db(); \
import sqlite3; c=kb.connect(); \
print([r[0] for r in c.execute(\"select name from sqlite_master where type='table'\")])"
```

`task_done_summaries` must be in that list.

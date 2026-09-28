"""Done Tasks — summary + AI review flag backend (Hermes plug-in).

Two halves, split across two cards:

* **This package** (``t_94fd868b``) — the ``kanban_task_completed`` hook that generates the
  stored digest for every completed task, plus a ``/done-summary`` slash command that runs
  the backfill sweep by hand. The heavy lifting lives in
  :mod:`hermes_cli.done_tasks_summary` so the logic is importable and testable without
  loading a plug-in.
* **HTTP surface** (``t_cfcf0794``) — ``dashboard/`` grows the ``/api/plugins/done-tasks/``
  routes (``GET /done``, ``GET /done/{task_id}``, ``POST /archive``, ``POST /unarchive``,
  ``POST /regenerate/{task_id}``) on top of the same module.

The hook fires AFTER the DB commit and is best-effort by contract (``kanban_db`` swallows
observer failures), so the callback never raises: a summarisation problem becomes a stored
fallback row, not a broken completion.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _on_kanban_task_completed(task_id: str = "", board=None, assignee=None,
                              run_id=None, profile_name=None, summary=None, **kwargs) -> None:
    """Generate the digest for the task that just completed. Never raises."""
    try:
        from hermes_cli import done_tasks_summary as dts

        result = dts.generate_done_summary(
            task_id=task_id, board=board, generated_by=f"hook:{profile_name or 'unknown'}")
        if result.get("ok"):
            log.debug("done-tasks: %s", dts.summarize_result(result))
        else:
            log.debug("done-tasks: skipped %s (%s)", task_id, result.get("reason"))
    except Exception as exc:  # pragma: no cover - the hook must never break a transition
        log.warning("done-tasks hook failed for %s: %s", task_id, exc)


def _on_kanban_dispatch_tick(board=None, profile_name=None, dry_run=False, outcome=None,
                             result=None, **kwargs) -> None:
    """Fleet-wide safety net: sweep unfinished digests from the dispatcher's own process.

    The completion hook only fires where this plug-in is loaded, and every Kanban worker runs
    under a profile-scoped HERMES_HOME — so a bot-authored completion could reach the board
    with no digest at all (card t_1d19e4b5: 122 done tasks, 0 rows). The dispatcher runs in
    the gateway, which is not a worker and owns the board, so it can summarise anything the
    fleet finished. Throttled to one bounded pass per minute and run off-thread, because a
    sweep can spend up to 45s per task in the summariser and a dispatcher tick must never wait.
    """
    if dry_run:
        return
    try:
        from hermes_cli import done_tasks_summary as dts

        dts.kick_background_sweep(board=board, generated_by="sweep:dispatch-tick")
    except Exception as exc:  # pragma: no cover - a hook must never break a tick
        log.debug("done-tasks dispatch-tick sweep skipped: %s", exc)


def _handle_slash(raw_args: str) -> str:
    """``/done-summary backfill|status [--limit N] [--board SLUG]``."""
    try:
        from hermes_cli import done_tasks_summary as dts
    except Exception as exc:
        return f"done-summary unavailable: {exc}"
    try:
        return dts.cli((raw_args or "").split())
    except Exception as exc:
        log.warning("done-summary command failed: %s", exc)
        return f"done-summary failed: {type(exc).__name__}: {exc}"


def register(ctx) -> None:
    """Plug-in entry point: completion hook + fleet sweep + manual backfill command."""
    ctx.register_hook("kanban_task_completed", _on_kanban_task_completed)
    # Fleet-wide safety net (card t_1d19e4b5): the dispatcher lives in the gateway, which is
    # not a fenced worker, so this covers completions from every bot profile.
    ctx.register_hook("on_kanban_dispatch_tick", _on_kanban_dispatch_tick)
    ctx.register_command(
        "done-summary", handler=_handle_slash,
        description="Done-task summaries: backfill the stored digests or show their status.",
        args_hint="backfill|status [--limit N] [--board SLUG]",
    )

"""Done Tasks plug-in — archive state, listing and the soft-archive write path.

Implements DONE-TASKS-PLUGIN-SPEC.md §1.2 (the ``task_done_summaries`` table lives in
``kanban_db.SCHEMA_SQL``), §4 (archive semantics) and §5.1/§5.3 (the ``GET /done`` and
``POST /archive|/unarchive|/regenerate`` surface).

The one rule that matters: **this module's archive is soft.** ``archive_state`` is the
plug-in's own flag (``active | archive_requested | archived``) and is *orthogonal* to
``tasks.status`` — a task may be ``status='done'`` and ``archive_state='archived'``
forever, and that is the normal end state.

Never call from here: ``kanban_db.archive_task`` (sets the terminal ``tasks.status='archived'``,
kills a running worker, reaps the workspace), ``delete_task``, ``delete_archived_task``.
There is no ``DELETE`` of any kind in this module: the feature's storage is append/update-only.

Sibling boundary: the summary + review-flag generator is ``hermes_cli.done_tasks_summary``
(task ``t_94fd868b``). It is imported **lazily inside functions** so this module works
standalone — when it is absent, a done task with no summary row is still listed (and still
archivable) with a named ``pending`` degradation rather than being hidden or blank.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

_log = logging.getLogger(__name__)

# --- Constants (frozen; see §5.1/§5.3) ---------------------------------------

ARCHIVE_ACTIVE = "active"
ARCHIVE_REQUESTED = "archive_requested"
ARCHIVE_ARCHIVED = "archived"
ARCHIVE_STATES = (ARCHIVE_ACTIVE, ARCHIVE_REQUESTED, ARCHIVE_ARCHIVED)

#: ``summary_source`` value for a done task the summary pass has not reached yet.
SOURCE_PENDING = "pending"

MAX_BATCH = 100            # §5.3: 1-100 task_ids per call
MAX_ACTOR_LEN = 200        # §5.3: actor trimmed to 200 chars
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass
class DoneTask:
    """One row of the done listing. ``raw`` is the DB row, or ``None`` for a pending task."""

    task_id: str
    title: str
    assignee: Optional[str]
    created_by: Optional[str]
    completed_at: Optional[int]
    summary: str
    summary_source: str
    artifacts: list[str] = field(default_factory=list)
    artifact_count: int = 0
    review_flag: bool = False
    review_reasons: list[str] = field(default_factory=list)
    review_reason: Optional[str] = None
    review_rule_version: Optional[str] = None
    archive_state: str = ARCHIVE_ACTIVE
    archive_requested_at: Optional[int] = None
    archive_requested_by: Optional[str] = None
    archived_at: Optional[int] = None
    archived_by: Optional[str] = None
    generated_at: Optional[int] = None
    fallback: bool = False

    @property
    def pending(self) -> bool:
        return self.summary_source == SOURCE_PENDING

    def to_dict(self) -> dict[str, Any]:
        """Response shape for §5.1 (exact key set; no summary-row internals leak)."""
        return {
            "task_id": self.task_id,
            "title": self.title,
            "assignee": self.assignee,
            "created_by": self.created_by,
            "completed_at": self.completed_at,
            "summary": self.summary,
            "summary_source": self.summary_source,
            "artifacts": list(self.artifacts),
            "artifact_count": self.artifact_count,
            "review_flag": bool(self.review_flag),
            "review_reasons": list(self.review_reasons),
            "review_reason": self.review_reason,
            "review_rule_version": self.review_rule_version,
            "archive_state": self.archive_state,
            "archive_requested_at": self.archive_requested_at,
            "archive_requested_by": self.archive_requested_by,
            "archived_at": self.archived_at,
            "archived_by": self.archived_by,
            "generated_at": self.generated_at,
            "fallback": bool(self.fallback),
            "pending": self.pending,
        }


# --- Row helpers -------------------------------------------------------------

def _ls_json(raw: Any) -> list:
    """Best-effort decode of a JSON-list column; a bad blob yields ``[]``, never raises."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    if not isinstance(raw, str):
        return []
    text = raw.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return []
    return list(parsed) if isinstance(parsed, (list, tuple)) else []


def _artifacts_of(raw: Any) -> list[str]:
    """String artifact paths from the stored JSON column (blank entries dropped)."""
    return [p for p in (str(x).strip() for x in _ls_json(raw)) if p]


def _now() -> int:
    return int(time.time())


def _get_row(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM task_done_summaries WHERE task_id = ?", (task_id,)
    ).fetchone()


def _row_to_done(conn: sqlite3.Connection, task_id: str, row: Optional[sqlite3.Row]) -> Optional[DoneTask]:
    """Build the listing object for one task.

    Returns ``None`` when the task is neither a summary row nor a done task (the caller's
    404). A done task with **no** summary row is returned as ``pending`` — never hidden,
    never blank (§5.1).
    """
    if row is None:
        task = kb.get_task(conn, task_id)
        if task is None or task.status != "done":
            return None
        return DoneTask(
            task_id=task.id,
            title=task.title or "",
            assignee=task.assignee,
            created_by=task.created_by,
            completed_at=task.completed_at,
            summary="",
            summary_source=SOURCE_PENDING,
            artifacts=[],
            artifact_count=0,
            review_flag=False,
            review_reasons=[],
            review_reason=None,
            review_rule_version=None,
            archive_state=ARCHIVE_ACTIVE,
            generated_at=None,
            fallback=True,
        )
    rules_raw = _ls_json(row["review_reasons"])
    reasons = [str(r) for r in rules_raw if str(r).strip()]
    flag = bool(row["review_flag"])
    source = row["summary_source"] or SOURCE_PENDING
    return DoneTask(
        task_id=row["task_id"],
        title=row["title"] or "",
        assignee=row["assignee"],
        created_by=row["created_by"] if "created_by" in row.keys() else None,
        completed_at=row["completed_at"],
        summary=row["summary"] or "",
        summary_source=source,
        artifacts=_artifacts_of(row["artifacts"]),
        artifact_count=int(row["artifact_count"] or 0),
        review_flag=flag,
        review_reasons=reasons,
        review_reason=row["review_reason"],
        review_rule_version=row["review_rule_version"],
        archive_state=row["archive_state"] or ARCHIVE_ACTIVE,
        archive_requested_at=row["archive_requested_at"],
        archive_requested_by=row["archive_requested_by"],
        archived_at=row["archived_at"],
        archived_by=row["archived_by"],
        generated_at=row["generated_at"],
        # A degraded summary is surfaced, never hidden (§2.3 / §5.4).
        fallback=bool(source in (SOURCE_PENDING, "fallback_title_body")),
    )


# --- Listing (§5.1) ----------------------------------------------------------

def _transient_pending(conn: sqlite3.Connection, task_id: str) -> Optional[DoneTask]:
    """A ``pending`` view for a done task that has no summary row (sibling module absent)."""
    task = kb.get_task(conn, task_id)
    if task is None or task.status != "done":
        return None
    return DoneTask(
        task_id=task.id, title=task.title or "", assignee=task.assignee,
        created_by=task.created_by, completed_at=task.completed_at,
        summary="", summary_source=SOURCE_PENDING, generated_at=None, fallback=True,
    )


def list_done(
    conn: sqlite3.Connection,
    *,
    include_archived: bool = False,
    only_review: bool = False,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """The default done listing: newest-completed first, archived soft-hidden.

    A LEFT JOIN keeps done tasks with no summary row in the result (``pending``), because
    hiding a card just because the sweep has not reached it is exactly the failure §5.1
    warns against.
    """
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    offset = max(0, int(offset or 0))

    where = ["t.status = 'done'", "t.completed_at IS NOT NULL"]
    params: list[Any] = []
    if not include_archived:
        # COALESCE: a done task with no summary row is un-archived by definition.
        where.append("COALESCE(s.archive_state, 'active') != ?")
        params.append(ARCHIVE_ARCHIVED)
    if only_review:
        where.append("s.review_flag = 1")
    clause = " AND ".join(where)

    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM tasks t"
        f" LEFT JOIN task_done_summaries s ON s.task_id = t.id WHERE {clause}",
        params,
    ).fetchone()["n"]

    rows = conn.execute(
        f"""
        SELECT s.*, t.id AS _tid, t.title AS _title, t.assignee AS _assignee,
               t.created_by AS _created_by, t.completed_at AS _completed_at
          FROM tasks t
          LEFT JOIN task_done_summaries s ON s.task_id = t.id
         WHERE {clause}
         ORDER BY t.completed_at DESC, t.id DESC
         LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()

    items: list[dict[str, Any]] = []
    for r in rows:
        if r["task_id"] is None:
            # No summary row: render from the task columns as `pending`.
            items.append(
                DoneTask(
                    task_id=r["_tid"], title=r["_title"] or "", assignee=r["_assignee"],
                    created_by=r["_created_by"], completed_at=r["_completed_at"],
                    summary="", summary_source=SOURCE_PENDING, generated_at=None, fallback=True,
                ).to_dict()
            )
            continue
        done = _row_to_done(conn, r["task_id"], r)
        if done is not None:
            # `created_by` is not denormalised into the summary table; take it live.
            done.created_by = r["_created_by"]
            if not done.title:
                done.title = r["_title"] or ""
            items.append(done.to_dict())

    return {
        "total": int(total),
        "returned": len(items),
        "items": items,
    }


def get_done(conn: sqlite3.Connection, task_id: str) -> Optional[DoneTask]:
    """One record regardless of ``archive_state`` (§5.2). ``None`` -> the caller's 404."""
    return _row_to_done(conn, task_id, _get_row(conn, task_id))


# --- Task facts shared by the detail view ------------------------------------

def build_detail(conn: sqlite3.Connection, done: DoneTask) -> dict[str, Any]:
    """§5.2 detail object: the listing entry plus body/runs/events/attachments/links.

    Reads only — ``tasks``, its runs, its events and its attachments are untouched by this
    feature at every point in the archive lifecycle.
    """
    task = kb.get_task(conn, done.task_id)
    out = done.to_dict()
    out.update(
        {
            "status": getattr(task, "status", None),
            "body": getattr(task, "body", None),
            "result": getattr(task, "result", None),
            "runs": [
                {
                    "id": r.id,
                    "status": r.status,
                    "outcome": r.outcome,
                    "summary": r.summary,
                    "ended_at": r.ended_at,
                }
                for r in (kb.list_runs(conn, done.task_id) if task else [])
            ],
            "events_tail": [
                {"kind": e.kind, "created_at": e.created_at}
                for e in (kb.list_events(conn, done.task_id) if task else [])
            ][-20:],
            "attachments": [
                {"id": a.id, "filename": a.filename, "size": a.size}
                for a in (kb.list_attachments(conn, done.task_id) if task else [])
            ],
            "prompt_version": "v1",
        }
    )
    return out


# --- Archive writes (§4 / §5.3) ---------------------------------------------

def _classify(
    conn: sqlite3.Connection, task_ids: Iterable[str], state: str,
) -> tuple[list[str], list[str], list[dict[str, str]], list[str]]:
    """Split requested ids into (rows_to_write, already_there, skipped, unknown).

    Only ``status='done'`` tasks may be archived: the owner's request is about the Done
    lane (§5.3). Unknown ids are reported, never a 404 for the whole call.
    """
    to_write: list[str] = []
    already: list[str] = []
    skipped: list[dict[str, str]] = []
    unknown: list[str] = []
    archiving = state != ARCHIVE_ACTIVE
    want = ARCHIVE_ARCHIVED if archiving else ARCHIVE_ACTIVE
    for raw_id in task_ids:
        tid = str(raw_id).strip()
        if not tid:
            continue
        row = _get_row(conn, tid)
        task = kb.get_task(conn, tid)
        if row is None and task is None:
            unknown.append(tid)
            continue
        if archiving and (task is None or task.status != "done"):
            skipped.append({"task_id": tid, "reason": "task is not done"})
            continue
        current = (row["archive_state"] if row is not None else None) or None
        # `already` means "the stored state already matches the target". That is only
        # knowable from a real row: a done task the sweep has not reached has NO state, and
        # reporting it as `already` would silently skip the write (and so leave the card in
        # the default listing). Such a task is materialised with safe placeholders instead.
        if row is not None and current == want:
            already.append(tid)
            continue
        to_write.append(tid)
    return to_write, already, skipped, unknown


def set_archive_state(
    conn: sqlite3.Connection,
    task_ids: Iterable[str],
    state: str,
    *,
    actor: str,
) -> dict[str, Any]:
    """Mark for archive / archive / unarchive (§4, §5.3).

    ``state='archive_requested'`` writes the request **and promotes it to ``archived`` in
    the same write**, keeping §4's ``active -> archive_requested -> archived`` chain
    schema-legal while never leaving a stuck intermediate row (which would silently vanish
    from a default view that only filters ``archived``). ``archived_at``/``archived_by``
    are stamped on promotion; the ``archive_requested_*`` pair preserves the original tap.

    ``state='active'`` is the reversal: it clears **all four** archive fields so a later
    re-archive starts clean and the timestamps never lie.

    Rows the summary pass has not created yet are **materialised with safe placeholders**,
    so a done task is archivable (and therefore hideable) before the sibling lands. An
    existing row's summary/review columns are never touched here.
    """
    if state not in ARCHIVE_STATES:
        raise ValueError(f"archive_state must be one of {list(ARCHIVE_STATES)}, got {state!r}")
    actor = " ".join(str(actor or "").split())
    if not actor:
        raise ValueError("actor is required")
    actor = actor[:MAX_ACTOR_LEN]

    ids = [str(t).strip() for t in task_ids if str(t).strip()]
    if not ids:
        raise ValueError("no task_ids supplied")
    if len(ids) > MAX_BATCH:
        raise ValueError(f"too many task_ids ({len(ids)}); max {MAX_BATCH}")
    # De-dupe, order-preserving.
    seen: set[str] = set()
    ids = [i for i in ids if not (i in seen or seen.add(i))]

    # Single promotion point: a request is written and promoted in the same write, so
    # `archive_requested` is schema-legal but never a *resting* state. Applying it here
    # (rather than in the upsert) keeps classify/insert/update all agreeing on one target,
    # which is what let a request settle as un-archived in the first cut of this module.
    stored_state = ARCHIVE_ARCHIVED if state == ARCHIVE_REQUESTED else state

    to_write, already, skipped, unknown = _classify(conn, ids, state)
    now = _now()
    written: list[str] = []

    if to_write:
        with kbc.write_txn(conn):
            for tid in to_write:
                _upsert_archive_row(conn, tid, stored_state, actor=actor, now=now)
                written.append(tid)

    if state == ARCHIVE_ACTIVE:
        body = {"ok": True, "unarchived": written, "already": already,
                "skipped": skipped, "unknown": unknown}
    else:
        body = {"ok": True, "archived": written, "already": already,
                "skipped": skipped, "unknown": unknown}
        if state == ARCHIVE_REQUESTED:
            body["requested"] = written
    return body


def _upsert_archive_row(
    conn: sqlite3.Connection, task_id: str, state: str, *, actor: str, now: int,
) -> None:
    """Write the archive columns for one task, creating the row if the sweep got there first.

    ``state`` is already promoted (see :func:`set_archive_state`): ``archived`` or
    ``active`` only.
    """
    row = _get_row(conn, task_id)
    if row is None:
        task = kb.get_task(conn, task_id)
        if task is None:
            return
        summary = _pending_summary_text(conn, task_id, task)
        conn.execute(
            """
            INSERT INTO task_done_summaries (
                task_id, title, assignee, completed_at, summary, summary_source,
                artifacts, artifact_count, review_flag, review_reasons, review_reason,
                review_rule_version, archive_state, archive_requested_at,
                archive_requested_by, archived_at, archived_by,
                generated_at, generated_by, attempt_count, last_error)
            VALUES (?, ?, ?, ?, ?, ?, '[]', 0, 0, '[]', NULL, 'v1', ?, ?, ?, ?, ?, ?, ?, 0, NULL)
            """,
            (
                task_id, task.title or "", task.assignee, task.completed_at or now,
                summary, SOURCE_PENDING,
                state,
                now if state == ARCHIVE_ARCHIVED else None,
                actor if state == ARCHIVE_ARCHIVED else None,
                now if state == ARCHIVE_ARCHIVED else None,
                actor if state == ARCHIVE_ARCHIVED else None,
                now, f"archive:{actor}",
            ),
        )
        return

    if state == ARCHIVE_ARCHIVED:
        # Stamp the promotion, and record the request only when this row has not already
        # been through one — so re-archiving after an unarchive (which clears the request
        # pair) starts a fresh, truthful audit trail instead of resurrecting the old one.
        if row["archive_requested_at"] is None:
            conn.execute(
                "UPDATE task_done_summaries SET archive_state = ?, archive_requested_at = ?, "
                "archive_requested_by = ?, archived_at = ?, archived_by = ? WHERE task_id = ?",
                (ARCHIVE_ARCHIVED, now, actor, now, actor, task_id),
            )
        else:
            conn.execute(
                "UPDATE task_done_summaries SET archive_state = ?, archived_at = ?, "
                "archived_by = ? WHERE task_id = ?",
                (ARCHIVE_ARCHIVED, now, actor, task_id),
            )
        return
    # Reversal: clear all four so a re-archive is clean.
    conn.execute(
        "UPDATE task_done_summaries SET archive_state = ?, archive_requested_at = NULL, "
        "archive_requested_by = NULL, archived_at = NULL, archived_by = NULL WHERE task_id = ?",
        (ARCHIVE_ACTIVE, task_id),
    )


def _pending_summary_text(conn: sqlite3.Connection, task_id: str, task: Any) -> str:
    """Never-blank text for a row this module had to create (the summary pass owns it).

    Prefers the newest worker handoff summary the board already has; else the title. The
    row stays ``pending`` with ``last_error=NULL`` so the sweep/regenerate still adopts it.
    """
    try:
        latest = kb.latest_summary(conn, task_id)
    except Exception:  # pragma: no cover - defensive
        latest = None
    text = (latest or "").strip() or (task.title or "").strip()
    return text or "(summary pending)"


def mark_for_archive(conn: sqlite3.Connection, task_ids: Iterable[str], *, actor: str) -> dict[str, Any]:
    """'Seen it, file it' — the one-action the board owner asked for (§4)."""
    return set_archive_state(conn, task_ids, ARCHIVE_REQUESTED, actor=actor)


def unarchive(conn: sqlite3.Connection, task_ids: Iterable[str], *, actor: str) -> dict[str, Any]:
    """Reversal path (§4): back to ``active``, all four archive fields cleared."""
    return set_archive_state(conn, task_ids, ARCHIVE_ACTIVE, actor=actor)


# --- Regenerate (§5.4) -------------------------------------------------------

def regenerate(conn: sqlite3.Connection, task_id: str, *, force: bool = False) -> dict[str, Any]:
    """Re-run the summary pass for one task, preserving ``archive_state``.

    Delegates to the sibling generator when present. Absent (or raising), the call reports
    ``ok: False`` with a named reason and HTTP 200 — the never-raise precedent of the
    estimate endpoint (§5.4). It never writes and never clears the archive flag.
    """
    row = _get_row(conn, task_id)
    task = kb.get_task(conn, task_id)
    if row is None and task is None:
        return {"ok": False, "task_id": task_id, "reason": "task not found"}
    if row is not None and (row["summary_source"] or "") == "manual" and not force:
        return {
            "ok": False,
            "task_id": task_id,
            "reason": "summary_source is 'manual'; pass {\"force\": true} to overwrite",
            "summary_source": "manual",
        }

    generator, why = _load_generator()
    if generator is None:
        return {
            "ok": False,
            "task_id": task_id,
            "reason": why,
            "summary_source": (row["summary_source"] if row is not None else SOURCE_PENDING),
        }
    try:
        result = generator(conn, task_id, force=force)
    except Exception as exc:  # never raise into the caller (§5.4)
        _log.debug("done-tasks regenerate failed for %s: %s", task_id, exc)
        fresh = _get_row(conn, task_id)
        return {
            "ok": False,
            "task_id": task_id,
            "reason": f"{type(exc).__name__}",
            "summary_source": (fresh["summary_source"] if fresh is not None else SOURCE_PENDING),
        }
    fresh = _get_row(conn, task_id)
    out: dict[str, Any] = {"ok": True, "task_id": task_id}
    if isinstance(result, dict):
        out.update({k: v for k, v in result.items() if k != "task_id"})
    if fresh is not None:
        out.setdefault("summary_source", fresh["summary_source"])
        out.setdefault("review_flag", bool(fresh["review_flag"]))
        out.setdefault("review_reasons", _ls_json(fresh["review_reasons"]))
        out.setdefault("archive_state", fresh["archive_state"])
    return out


def _load_generator():
    """``(callable | None, why)`` for ``hermes_cli.done_tasks_summary.generate_done_summary``.

    Imported lazily so this module (and the archive feature) works before — and
    independently of — the sibling summary task.
    """
    try:
        from hermes_cli import done_tasks_summary  # type: ignore
    except Exception:
        return None, "summary generator (hermes_cli.done_tasks_summary) is not installed"
    fn = getattr(done_tasks_summary, "generate_done_summary", None)
    if not callable(fn):
        return None, "summary generator exposes no generate_done_summary()"
    return fn, ""


__all__ = [
    "ARCHIVE_ACTIVE",
    "ARCHIVE_REQUESTED",
    "ARCHIVE_ARCHIVED",
    "ARCHIVE_STATES",
    "SOURCE_PENDING",
    "MAX_BATCH",
    "MAX_ACTOR_LEN",
    "DoneTask",
    "list_done",
    "get_done",
    "build_detail",
    "set_archive_state",
    "mark_for_archive",
    "unarchive",
    "regenerate",
]

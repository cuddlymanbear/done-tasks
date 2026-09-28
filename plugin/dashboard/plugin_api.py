"""Done Tasks plug-in — the HTTP surface (§5 of DONE-TASKS-PLUGIN-SPEC.md).

Mounted at ``/api/plugins/done-tasks/``. Four endpoints, as reserved by this package's
``__init__`` docstring for card ``t_cfcf0794``:

* ``GET  /done``              — the digest listing (archived hidden unless asked for)
* ``GET  /done/{task_id}``    — one record, always, whatever its ``archive_state``
* ``POST /archive``           — 'seen it, file it'  (+ ``POST /unarchive`` to reverse)
* ``POST /regenerate/{id}``   — re-run the summary pass

Board resolution matches the kanban plug-in exactly: malformed slug -> 400, unknown board
-> 404, omitted -> the active board.

All archive semantics live in ``hermes_cli.done_tasks_archive`` — this module only shapes
requests and responses. Nothing here touches ``tasks.status``.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import done_tasks_archive as dta

log = logging.getLogger(__name__)

router = APIRouter()

_BOARD_Q = Query(None, description="Kanban board slug (omit for current)")


# --- Board resolution (same contract as the kanban plug-in) -------------------

def _resolve_board(board: Optional[str]) -> Optional[str]:
    """Validate/normalise a board slug (400 malformed, 404 unknown); ``None`` when omitted."""
    if board is None or board == "":
        return None
    try:
        normed = kb._normalize_board_slug(board)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if normed and normed != kb.DEFAULT_BOARD and not kb.board_exists(normed):
        raise HTTPException(status_code=404, detail=f"board {normed!r} does not exist")
    return normed


def _conn(board: Optional[str] = None) -> sqlite3.Connection:
    """Connect to the normalised ``board`` (``None`` = active); ``init_db`` self-heals."""
    try:
        kb.init_db(board=board)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("done-tasks init_db failed: %s", exc)
    return kbc.connect(board=board)


@contextmanager
def _board_conn(board: Optional[str]) -> Iterator[tuple[Optional[str], sqlite3.Connection]]:
    resolved = _resolve_board(board)
    conn = _conn(board=resolved)
    try:
        yield resolved, conn
    finally:
        conn.close()


# --- Request bodies ----------------------------------------------------------

class ArchiveBody(BaseModel):
    """Body of ``POST /archive`` and ``POST /unarchive``."""

    task_ids: list[str] = Field(default_factory=list)
    # Required and never defaulted: the archive fields record who acted, and a guessed
    # actor makes the audit trail a lie (§5.3).
    actor: str = ""


class RegenerateBody(BaseModel):
    force: bool = False


def _body_error(reason: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason}


def _run_archive(body: ArchiveBody, board: Optional[str], *, state: str) -> dict[str, Any]:
    """Shared handler for archive / unarchive; maps ``ValueError`` to a 400 per §5.3."""
    if not [t for t in (body.task_ids or []) if str(t).strip()]:
        raise HTTPException(status_code=400, detail=_body_error("no task_ids supplied"))
    if not str(body.actor or "").strip():
        raise HTTPException(status_code=400, detail=_body_error("actor is required"))
    with _board_conn(board) as (_, conn):
        try:
            return dta.set_archive_state(conn, body.task_ids, state, actor=body.actor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=_body_error(str(exc))) from exc


# --- Endpoints ---------------------------------------------------------------

@router.get("/done")
def get_done_list(
    board: Optional[str] = _BOARD_Q,
    limit: int = Query(dta.DEFAULT_LIMIT, ge=1, le=dta.MAX_LIMIT),
    offset: int = Query(0, ge=0),
    include_archived: bool = Query(False, description="Include soft-archived records"),
    only_review: bool = Query(False, description="Only records flagged for review"),
) -> dict[str, Any]:
    """The default done listing. Archived records are excluded unless asked for."""
    with _board_conn(board) as (resolved, conn):
        payload = dta.list_done(
            conn,
            include_archived=include_archived,
            only_review=only_review,
            limit=limit,
            offset=offset,
        )
        payload["board"] = resolved or kb.get_current_board()
        payload["now"] = dta._now()
        payload["limit"] = limit
        payload["offset"] = offset
        payload["include_archived"] = bool(include_archived)
        payload["only_review"] = bool(only_review)
        return payload


@router.get("/done/{task_id}")
def get_done_detail(task_id: str, board: Optional[str] = _BOARD_Q) -> dict[str, Any]:
    """One record regardless of ``archive_state``; 404 when it is not a done task either."""
    with _board_conn(board) as (_, conn):
        done = dta.get_done(conn, task_id)
        if done is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        return dta.build_detail(conn, done)


@router.post("/archive")
def post_archive(body: ArchiveBody, board: Optional[str] = _BOARD_Q) -> dict[str, Any]:
    """Mark done tasks for archive ('seen it, file it'). Soft, reversible, idempotent."""
    return _run_archive(body, board, state=dta.ARCHIVE_REQUESTED)


@router.post("/unarchive")
def post_unarchive(body: ArchiveBody, board: Optional[str] = _BOARD_Q) -> dict[str, Any]:
    """Reverse an archive: back to ``active``, all four archive fields cleared."""
    return _run_archive(body, board, state=dta.ARCHIVE_ACTIVE)


@router.post("/regenerate/{task_id}")
def post_regenerate(
    task_id: str,
    body: RegenerateBody | None = None,
    board: Optional[str] = _BOARD_Q,
) -> dict[str, Any]:
    """Re-run the summary pass for one task. Never raises: failures are a 200 + ok:false."""
    force = bool(getattr(body, "force", False))
    with _board_conn(board) as (_, conn):
        return dta.regenerate(conn, task_id, force=force)

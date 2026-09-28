"""Done Tasks plug-in — summary generation + deterministic AI review flag.

Backend half of the "Done Tasks" summary plug-in (sibling card ``t_cfcf0794`` owns the
archive endpoints, ``t_5f4fcaf7`` the UI). Implements ``DONE-TASKS-PLUGIN-SPEC.md`` §1–§3
and §5.5: one stored digest per completed task, a pure-rule review flag that never needs an
LLM, and a graceful degradation ladder so a done card is never blank and never hidden.

Three entry points matter:

* :func:`generate_done_summary` — one task, idempotent, never raises. Called from the
  ``kanban_task_completed`` lifecycle hook (post-commit) and from the sweep.
* :func:`generate_pending` — the sweep body: backfills done tasks with no row, retries
  fallback/errored rows (bounded by ``MAX_ATTEMPTS``).
* :func:`evaluate_review_rules` — the pure rule engine (R1–R9). No I/O, no clock, no LLM,
  so a rule change is a one-line test change.

The one fact that silently breaks naive implementations (spec §0): ``tasks.result`` is NULL
on every recent done task — the worker's handoff text lives in ``task_runs.summary``, so this
module reads it through :func:`hermes_cli.kanban_db.latest_summary`.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as kb

log = logging.getLogger(__name__)

# --- Versioning -------------------------------------------------------------
# Rule ids are FROZEN strings; adding a rule bumps REVIEW_RULE_VERSION (spec §3).
REVIEW_RULE_VERSION = "v1"
PROMPT_VERSION = "v1"
SUMMARY_MAX_CHARS = 600
REVIEW_REASON_MAX_CHARS = 200

#: Sweep attempts before a row is left on its fallback summary for good.
MAX_ATTEMPTS = 3
#: Rows the sweep touches per call (bounded, matching the spec's SELECT).
SWEEP_LIMIT = 25

SUMMARY_SOURCE_LLM = "llm"
SUMMARY_SOURCE_REPAIRED = "llm_repaired"
SUMMARY_SOURCE_FALLBACK = "fallback_title_body"
SUMMARY_SOURCE_MANUAL = "manual"
#: Never written by the generator — the read API synthesises it for a done task whose
#: digest the sweep has not reached yet, so the UI can render "pending" instead of blank.
SUMMARY_SOURCE_PENDING = "pending"

VALID_ARCHIVE_STATES = ("active", "archive_requested", "archived")


# --- Review rules (spec §3) -------------------------------------------------

# R2 — touched money.
_MONEY_RE = re.compile(
    r"(?i)\b(money|invoice|invoic|payment|refund|credit|charge|price|pricing|quote"
    r"|estimate_amount|purchase|billing|payable|receivable|deposit)\b"
    r"|\$\s?\d"
)
# R3 — changed live config or credential material.
_CONFIG_RE = re.compile(
    r"\.env|config\.yaml|credentials|secret|password|token|api[_-]?key"
    r"|systemd/|/etc/|crontab|authorized_keys|sudoers",
    re.IGNORECASE,
)
# R4 — an explicit "there was nothing to build" opt-out.
_NO_BUILD_RE = re.compile(r"(?i)\b(self-test|no build|nothing to build)\b")
# R6 — destructive vocabulary.
_DESTRUCTIVE_RE = re.compile(
    r"(?i)\b(delete|deleted|drop|dropped|truncate|purge|wipe|rm -rf|overwrite|overwrote"
    r"|migrat|migrated|reset|rollback|rotat|rotate)\b"
)
# R7 — a path inside another profile's tree.
_PROFILE_PATH_RE = re.compile(r"^/home/[^/]+/\.hermes/profiles/([^/]+)/")

#: ``task_runs.status`` values that mean "this run did not finish cleanly" (R5).
FAILED_RUN_STATUSES = frozenset({
    "crashed", "timed_out", "failed", "reclaimed", "gave_up", "spawn_failed",
})
#: ``task_runs.outcome`` equivalents (older rows carry only the outcome).
FAILED_RUN_OUTCOMES = frozenset({
    "crashed", "timed_out", "failed", "gave_up", "spawn_failed", "reclaimed",
})

#: Fired rule id -> the badge sentence shown to the owner. Rule ORDER is priority order:
#: ``review_reason`` is the sentence of the first fired rule.
RULE_SENTENCES: dict[str, str] = {
    "R1": "Large job — a lot changed at once.",
    "R2": "Touches money or pricing.",
    "R3": "Changed live configuration or credential material.",
    "R4": "Finished without producing a file or attachment.",
    "R5": "Had to be retried or restarted before it finished.",
    "R6": "Talks about deleting, overwriting or rotating something.",
    "R7": "Changed files belonging to another bot.",
    "R8": "The summariser flagged this for a look.",
    "R9": "Wrote outside the normal workspace.",
}
RULE_ORDER: tuple[str, ...] = tuple(RULE_SENTENCES)


@dataclass
class DoneTaskContext:
    """Everything the rules may look at. Pure data — no connection, no clock.

    ``started_at`` / ``completed_at`` / ``children_count`` / ``attachment_count`` /
    ``workspaces_root`` are pre-resolved by :func:`build_context` so a rule can never
    reach for I/O of its own (that is what makes the rule table unit-testable).
    """

    title: str = ""
    body: Optional[str] = None
    created_by: Optional[str] = None
    assignee: Optional[str] = None
    summary: Optional[str] = None
    artifacts: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    session_id: Optional[str] = None
    started_at: Optional[int] = None
    completed_at: Optional[int] = None
    children_count: int = 0
    attachment_count: int = 0
    workspaces_root: Optional[str] = None
    consecutive_failures: int = 0
    attempt_index: int = 1


def _haystack(*parts: Any) -> str:
    """Join text-bearing parts into one lowercase-searchable blob (rule input helper)."""
    out: list[str] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, (list, tuple, set)):
            out.extend(str(p) for p in part if p)
        else:
            out.append(str(part))
    return "\n".join(out)


def _all_paths(ctx: DoneTaskContext) -> list[str]:
    """Artifact + declared-changed paths, deduped, order-preserving."""
    seen: dict[str, None] = {}
    for p in list(ctx.artifacts) + list(ctx.changed_files):
        if isinstance(p, str) and p.strip():
            seen.setdefault(p.strip(), None)
    return list(seen)


def evaluate_review_rules(ctx: DoneTaskContext) -> tuple[bool, list[str], Optional[str]]:
    """Pure rule engine: ``(review_flag, fired_rule_ids, first_sentence)``.

    Deterministic by construction — same context in, identical tuple out, no I/O, no clock
    and no LLM. R8 (the model's advisory hint) can only ADD a rule, never clear one, which
    is why the deterministic hits are collected first and the result is always derived from
    the fired set rather than from any single rule's short-circuit.
    """
    fired: list[str] = []

    # R1 — changed something big.
    if ctx.started_at is not None and ctx.completed_at is not None:
        if int(ctx.completed_at) - int(ctx.started_at) > 3600:
            fired.append("R1")
    if "R1" not in fired and ctx.children_count >= 3:
        # The task is a parent of a fan-out.
        fired.append("R1")

    body_title_summary = _haystack(ctx.title, ctx.body, ctx.summary)

    # R2 — touched money.
    if _MONEY_RE.search(body_title_summary):
        fired.append("R2")

    # R3 — changed live config or secrets.
    if _CONFIG_RE.search(_haystack(body_title_summary, ctx.changed_files, ctx.artifacts)):
        fired.append("R3")

    # R4 — produced nothing.
    if ctx.artifact_count_zero and ctx.attachment_count == 0 and not _NO_BUILD_RE.search(
        _haystack(ctx.title, ctx.body)
    ):
        fired.append("R4")

    # R5 — was re-run / restarted.
    retried = ctx.attempt_index > 1 or int(ctx.consecutive_failures or 0) > 0
    if not retried:
        for run in ctx.runs or []:
            if not isinstance(run, dict):
                continue
            status = str(run.get("status") or "")
            outcome = str(run.get("outcome") or "")
            if status in FAILED_RUN_STATUSES or outcome in FAILED_RUN_OUTCOMES:
                retried = True
                break
    if not retried:
        seen_archived = False
        for kind in ctx.events or []:
            if kind == "archived":
                seen_archived = True
            elif kind == "completed" and seen_archived:
                retried = True  # archived, then completed again
                break
    if retried:
        fired.append("R5")

    # R6 — destructive vocabulary.
    if _DESTRUCTIVE_RE.search(body_title_summary):
        fired.append("R6")

    # R7 — edited someone else's tree.
    for path in _all_paths(ctx):
        m = _PROFILE_PATH_RE.match(path)
        if m and ctx.assignee and m.group(1) != ctx.assignee:
            fired.append("R7")
            break

    # R8 — the model's own advisory hint (ADD-only; the hint text is the reason).
    hint = (ctx.metadata or {}).get("review_hint")
    hint_text: Optional[str] = None
    if isinstance(hint, str) and hint.strip():
        hint_text = _redact(hint.strip())[:REVIEW_REASON_MAX_CHARS]
        fired.append("R8")

    # R9 — work landed outside the sandbox.
    if ctx.session_id and ctx.workspaces_root:
        root = str(ctx.workspaces_root).rstrip("/")
        for path in _all_paths(ctx):
            if path.startswith("/") and not path.startswith(root + "/") and path != root:
                fired.append("R9")
                break

    fired = sorted(set(fired), key=RULE_ORDER.index)
    if not fired:
        return False, [], None
    reason = hint_text if fired == ["R8"] and hint_text else RULE_SENTENCES[fired[0]]
    return True, fired, reason


# ``artifacts`` is a plain list on the dataclass; R4 wants "no artifacts AND no
# attachments". Exposed as a property so the dataclass stays constructible from dicts.
def _ctx_artifact_count_zero(self: DoneTaskContext) -> bool:
    return len([p for p in (self.artifacts or []) if isinstance(p, str) and p.strip()]) == 0


DoneTaskContext.artifact_count_zero = property(_ctx_artifact_count_zero)  # type: ignore[attr-defined]


# --- Redaction --------------------------------------------------------------

_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|password|passwd|secret|bearer)\b\s*[:=]\s*\S+"
)
_SK_TOKEN_RE = re.compile(r"\b(sk|pk|ghp|gho|xox[baprs])[-_][A-Za-z0-9_\-]{8,}")


def _redact(text: str) -> str:
    """Defensive scrub of model output before it is stored/displayed (spec §3 R8).

    The model is asked for one sentence and never given raw logs, but a stored summary is
    rendered in a dashboard, so a credential-shaped string must not survive the trip.
    """
    out = _SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}: [redacted]", text)
    return _SK_TOKEN_RE.sub("[redacted]", out)


# --- Artifact extraction (spec §2.4) ----------------------------------------

def _coerce_path_list(raw: Any) -> list[str]:
    """Anything -> list of path strings. Tolerates the shapes seen on the live board.

    ``metadata["artifacts"]`` has been observed as a real list, a JSON string holding a
    list, and a **stringified Python list** (``"['/a/b', '/c/d']"``). It is never allowed to
    raise: a task must not become unsavable because it declared its artifacts badly.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(p).strip() for p in raw if str(p).strip()]
    if isinstance(raw, dict):
        out: list[str] = []
        for value in raw.values():
            out.extend(_coerce_path_list(value))
        return out
    if not isinstance(raw, str):
        return []
    text = raw.strip()
    if not text:
        return []
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(text)
        except Exception:
            continue
        if isinstance(parsed, str):
            return [parsed.strip()] if parsed.strip() else []
        if isinstance(parsed, (list, tuple, set)):
            return [str(p).strip() for p in parsed if str(p).strip()]
        if isinstance(parsed, dict):
            return _coerce_path_list(parsed)
        return []
    # Last resort: one path per line (a plain text blob).
    return [line.strip() for line in text.splitlines() if line.strip()]


def extract_artifacts(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """Union of the three artifact sources, deduped by path/URL, capped at 25 (spec §2.4).

    Sources: the newest ``task_runs.metadata["artifacts"]``, the ``completed`` event
    payload's ``artifacts``, and ``task_attachments.stored_path``. A malformed source
    contributes nothing rather than raising.
    """
    found: list[str] = []

    try:
        for run in reversed(kb.list_runs(conn, task_id) or []):
            metadata = getattr(run, "metadata", None)
            if isinstance(metadata, dict):
                found.extend(_coerce_path_list(metadata.get("artifacts")))
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("artifact scan (runs) failed for %s: %s", task_id, exc)

    try:
        for event in kb.list_events(conn, task_id) or []:
            if getattr(event, "kind", None) != "completed":
                continue
            payload = getattr(event, "payload", None)
            if isinstance(payload, dict):
                found.extend(_coerce_path_list(payload.get("artifacts")))
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("artifact scan (events) failed for %s: %s", task_id, exc)

    try:
        for att in kb.list_attachments(conn, task_id) or []:
            path = getattr(att, "stored_path", None)
            if isinstance(path, str) and path.strip():
                found.append(path.strip())
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("artifact scan (attachments) failed for %s: %s", task_id, exc)

    seen: dict[str, None] = {}
    for path in found:
        if path:
            seen.setdefault(path, None)
    return list(seen)[:25]


def _changed_files(task_id: str) -> list[str]:
    """``changed_files`` from the newest run's metadata (a stringified list is tolerated)."""
    try:
        return _coerce_meta_list(task_id, "changed_files")
    except Exception:  # pragma: no cover - defensive
        return []


def _coerce_meta_list(task_id: str, key: str) -> list[str]:
    conn = _hook_conn()
    try:
        for run in reversed(kb.list_runs(conn, task_id) or []):
            metadata = getattr(run, "metadata", None)
            if isinstance(metadata, dict) and metadata.get(key):
                return _coerce_path_list(metadata.get(key))
    finally:
        conn.close()
    return []


# --- Context assembly -------------------------------------------------------

def build_context(conn: sqlite3.Connection, task: Any, *, board: Optional[str] = None,
                  summary_override: Optional[str] = None, metadata: Optional[dict] = None) -> DoneTaskContext:
    """Assemble the pure context for ``task`` (one task, bounded queries)."""
    task_id = task.id
    artifacts = extract_artifacts(conn, task_id)
    runs_raw = []
    try:
        for run in kb.list_runs(conn, task_id) or []:
            runs_raw.append({
                "id": getattr(run, "id", None),
                "status": getattr(run, "status", None),
                "outcome": getattr(run, "outcome", None),
                "summary": getattr(run, "summary", None),
                "ended_at": getattr(run, "ended_at", None),
            })
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("run scan failed for %s: %s", task_id, exc)
    events: list[str] = []
    try:
        events = [str(getattr(e, "kind", "")) for e in (kb.list_events(conn, task_id) or [])]
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("event scan failed for %s: %s", task_id, exc)

    meta: dict = {}
    for run in reversed(runs_raw):
        pass
    try:
        latest_meta = None
        for run in kb.list_runs(conn, task_id) or []:
            md = getattr(run, "metadata", None)
            if isinstance(md, dict):
                latest_meta = md
        meta = dict(latest_meta or {})
    except Exception:  # pragma: no cover - defensive
        meta = {}
    if metadata:
        meta.update(metadata)

    attachments = 0
    try:
        attachments = len(kb.list_attachments(conn, task_id) or [])
    except Exception:  # pragma: no cover - defensive
        attachments = 0

    children = 0
    try:
        with kb.write_txn(conn, allow_nested=True):
            pass
    except Exception:
        pass
    try:
        children = len(_children_for(conn, task_id))
    except Exception:  # pragma: no cover - defensive
        children = 0

    try:
        workspaces_root = str(kb.workspaces_root(board))
    except Exception:  # pragma: no cover - defensive
        workspaces_root = None

    return DoneTaskContext(
        title=task.title or "",
        body=task.body,
        created_by=task.created_by,
        assignee=task.assignee,
        summary=summary_override if summary_override is not None else kb.latest_summary(conn, task_id),
        artifacts=artifacts,
        changed_files=_coerce_meta_list(task_id, "changed_files"),
        runs=runs_raw,
        events=events,
        metadata=meta,
        session_id=getattr(task, "session_id", None),
        started_at=getattr(task, "started_at", None),
        completed_at=getattr(task, "completed_at", None),
        children_count=children,
        attachment_count=attachments,
        workspaces_root=workspaces_root,
        consecutive_failures=int(getattr(task, "consecutive_failures", 0) or 0),
    )


def _children_for(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """Child task ids of ``task_id`` from ``task_links``."""
    try:
        rows = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (task_id,)
        ).fetchall()
    except sqlite3.Error:  # pragma: no cover - schema always has the table
        return []
    return [r[0] for r in rows]


# --- LLM pass (spec §2.3) ---------------------------------------------------

SUMMARY_SYSTEM_PROMPT = (
    "You summarise one completed software/ops task for a non-technical shop owner. "
    "Reply with JSON only: {\"summary\": string, \"review_hint\": string|null}. "
    "`summary` is 1-3 short sentences, plain words, no file paths, no ids, no tool names, "
    "no markdown, max 600 characters. Say what was actually done and what it means. "
    "`review_hint` is null unless the owner should personally look at this; if so, one "
    "short sentence saying why."
)


def _cap(text: Optional[str], limit: int) -> str:
    s = (text or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _call_summary_llm(ctx: DoneTaskContext, task_id: str) -> tuple[Optional[dict], Optional[str]]:
    """Run the side-task LLM pass; ``(parsed_json_or_None, error_name_or_None)``.

    Mirrors ``plugin_api._run_estimate`` exactly: the shipped auxiliary router, temperature
    0, and the headless affinity scope (without a bound scope the relay answers ``400
    MissingSessionID``). No new config key. The whole call is wrapped so that even an
    unexpected exception type degrades to a fallback instead of escaping into the hook.
    """
    try:
        return _call_summary_llm_inner(ctx, task_id)
    except Exception as exc:
        return None, type(exc).__name__
    except BaseException as exc:  # pragma: no cover - defensive (hook must never raise)
        return None, type(exc).__name__


def _call_summary_llm_inner(ctx: DoneTaskContext, task_id: str) -> tuple[Optional[dict], Optional[str]]:
    try:
        from agent.auxiliary_client import call_llm
    except Exception:
        return None, "auxiliary_client_unavailable"

    artifact_count = len([p for p in (ctx.artifacts or []) if p])
    runs_line = f"{len(ctx.runs or [])}"
    bad = [str(r.get("outcome") or r.get("status") or "?") for r in (ctx.runs or [])
           if isinstance(r, dict) and (str(r.get("status") or "") in FAILED_RUN_STATUSES
                                       or str(r.get("outcome") or "") in FAILED_RUN_OUTCOMES)]
    if bad:
        runs_line += " (non-completed: " + ", ".join(sorted(set(bad))) + ")"
    user_msg = (
        f"Title: {_cap(ctx.title, 400)}\n\n"
        f"Body:\n{_cap(ctx.body, 4000) or '(none)'}\n\n"
        f"Assignee: {ctx.assignee or '(unassigned)'}\n\n"
        f"Final handoff summary:\n{_cap(ctx.summary, 4000) or '(none)'}\n\n"
        f"Artifact count: {artifact_count}\n"
        f"Runs: {runs_line}"
    )

    from agent.portal_tags import get_affinity_scope, reset_affinity_scope, set_affinity_scope

    affinity_token = None if get_affinity_scope() else set_affinity_scope(f"kanban:{task_id}")
    try:
        resp = call_llm(
            task="kanban_done_summary",
            messages=[{"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                      {"role": "user", "content": user_msg}],
            temperature=0.0, max_tokens=320, timeout=45)
    except Exception as exc:
        return None, type(exc).__name__
    except BaseException as exc:  # KeyboardInterrupt in a probe must not escape either
        return None, type(exc).__name__
    finally:
        if affinity_token is not None:
            reset_affinity_scope(affinity_token)

    try:
        raw = (resp.choices[0].message.content or "").strip()
    except Exception:
        raw = ""
    try:
        m = None if raw.lstrip().startswith("{") else re.search(r"\{.*\}", raw, re.DOTALL)
        obj = json.loads(m.group(0) if m else raw)
        parsed = obj if isinstance(obj, dict) else None
    except Exception:
        return None, "parse_failed"
    if not parsed:
        return None, "parse_failed"
    return parsed, None


def _fallback_summary(title: Optional[str], why: str) -> str:
    """Usable summary when the model is unavailable (spec §2.3) — never blank."""
    head = kb._first_line(title, 200) or "(untitled task)"
    return f"{head} [AI summary unavailable — {why}]"[:SUMMARY_MAX_CHARS + 80]


def _why_text(error: Optional[str]) -> str:
    mapping = {
        "parse_failed": "unreadable model reply",
        "empty_summary": "empty model reply",
        "auxiliary_client_unavailable": "no summariser available",
    }
    if error in mapping:
        return mapping[error]
    if error and error.isidentifier():
        return "summariser error"
    return "summariser unavailable"


# --- Persistence ------------------------------------------------------------

_INSERT_SQL = """
INSERT INTO task_done_summaries (
    task_id, title, assignee, completed_at, summary, summary_source, artifacts,
    artifact_count, review_flag, review_reasons, review_reason, review_rule_version,
    generated_at, generated_by, attempt_count, last_error
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(task_id) DO UPDATE SET
    title               = excluded.title,
    assignee            = excluded.assignee,
    completed_at        = excluded.completed_at,
    summary             = excluded.summary,
    summary_source      = excluded.summary_source,
    artifacts           = excluded.artifacts,
    artifact_count      = excluded.artifact_count,
    review_flag         = excluded.review_flag,
    review_reasons      = excluded.review_reasons,
    review_reason       = excluded.review_reason,
    review_rule_version = excluded.review_rule_version,
    generated_at        = excluded.generated_at,
    generated_by        = excluded.generated_by,
    attempt_count       = task_done_summaries.attempt_count + 1,
    last_error          = excluded.last_error
"""

_SELECT_COLUMNS = (
    "task_id, title, assignee, completed_at, summary, summary_source, artifacts, "
    "artifact_count, review_flag, review_reasons, review_reason, review_rule_version, "
    "archive_state, archive_requested_at, archive_requested_by, archived_at, archived_by, "
    "generated_at, generated_by, attempt_count, last_error"
)


def _decode_row(row: sqlite3.Row) -> dict:
    """Stored row -> API dict (JSON columns decoded, ints as bools)."""
    def _json_list(value: Any) -> list:
        try:
            parsed = json.loads(value or "[]")
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []

    return {
        "task_id": row["task_id"],
        "title": row["title"],
        "assignee": row["assignee"],
        "completed_at": row["completed_at"],
        "summary": row["summary"],
        "summary_source": row["summary_source"],
        "artifacts": _json_list(row["artifacts"]),
        "artifact_count": int(row["artifact_count"] or 0),
        "review_flag": bool(row["review_flag"]),
        "review_reasons": _json_list(row["review_reasons"]),
        "review_reason": row["review_reason"],
        "review_rule_version": row["review_rule_version"],
        "archive_state": row["archive_state"],
        "archive_requested_at": row["archive_requested_at"],
        "archive_requested_by": row["archive_requested_by"],
        "archived_at": row["archived_at"],
        "archived_by": row["archived_by"],
        "generated_at": row["generated_at"],
        "generated_by": row["generated_by"],
        "attempt_count": int(row["attempt_count"] or 1),
        "last_error": row["last_error"],
        "fallback": row["summary_source"] in (SUMMARY_SOURCE_FALLBACK, SUMMARY_SOURCE_PENDING),
    }


def get_record(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """Stored digest for ``task_id`` or ``None``."""
    row = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM task_done_summaries WHERE task_id = ?", (task_id,)
    ).fetchone()
    return _decode_row(row) if row else None


def _hook_conn(board: Optional[str] = None) -> sqlite3.Connection:
    """Connection for the hook path (``None`` board -> the active board)."""
    from hermes_cli import kanban_db_connect as kbc
    return kbc.connect(board=board)


def generate_done_summary(conn: Optional[sqlite3.Connection] = None, task_id: str = "",
                          *, board: Optional[str] = None, force: bool = False,
                          generated_by: str = "hook") -> dict:
    """Generate (or refresh) the digest row for one task. Never raises. Idempotent.

    Safe to call from inside the ``kanban_task_completed`` lifecycle hook: it runs after the
    DB commit, and every failure is swallowed into ``last_error`` on the row (or into the
    returned dict when even that write fails) so a hook callback can never break a
    transition.
    """
    own_conn = conn is None
    c = conn or _hook_conn(board)
    try:
        return _generate_locked(c, task_id, board=board, force=force, generated_by=generated_by)
    except Exception as exc:
        log.warning("done-summary generation failed for %s: %s", task_id, exc)
        return {"ok": False, "task_id": task_id, "reason": type(exc).__name__}
    finally:
        if own_conn:
            try:
                c.close()
            except Exception:  # pragma: no cover - defensive
                pass


def _generate_locked(conn: sqlite3.Connection, task_id: str, *, board: Optional[str],
                     force: bool, generated_by: str) -> dict:
    task = kb.get_task(conn, task_id)
    if task is None:
        return {"ok": False, "task_id": task_id, "reason": "unknown task"}
    if task.status != "done":
        return {"ok": False, "task_id": task_id, "reason": "task is not done",
                "skipped": True}

    existing = get_record(conn, task_id)
    if existing and existing["summary_source"] == SUMMARY_SOURCE_MANUAL and not force:
        return {"ok": True, "task_id": task_id, "skipped": True,
                "reason": "manual summary preserved", **existing}

    ctx = build_context(conn, task, board=board)

    summary: Optional[str] = None
    source = SUMMARY_SOURCE_LLM
    error: Optional[str] = None
    try:
        parsed, llm_error = _call_summary_llm(ctx, task_id)
    except BaseException as exc:  # pragma: no cover - defensive
        # The hook must never raise: an LLM/transport surprise degrades to the fallback.
        parsed, llm_error = None, type(exc).__name__
    if llm_error:
        error = llm_error
    elif parsed is not None:
        text = parsed.get("summary")
        text = text.strip() if isinstance(text, str) else ""
        if len(text) < 15:
            error = "empty_summary"
        else:
            summary = _redact(text)[:SUMMARY_MAX_CHARS]
            source = SUMMARY_SOURCE_LLM
            hint = parsed.get("review_hint")
            if isinstance(hint, str) and hint.strip():
                ctx.metadata["review_hint"] = hint.strip()

    if summary is None:
        source = SUMMARY_SOURCE_FALLBACK
        summary = _fallback_summary(task.title, _why_text(error))

    flag, reasons, reason = evaluate_review_rules(ctx)

    now = int(time.time())
    prior_attempts = int(existing["attempt_count"]) if existing else 0
    attempt_count = prior_attempts + 1 if existing else 1
    row = (
        task_id,
        task.title or "",
        task.assignee,
        int(task.completed_at or now),
        summary,
        source,
        json.dumps(list(ctx.artifacts)),
        len(ctx.artifacts),
        1 if flag else 0,
        json.dumps(reasons),
        (reason or "")[:REVIEW_REASON_MAX_CHARS] or None,
        REVIEW_RULE_VERSION,
        now,
        generated_by,
        attempt_count,
        error,
    )
    with kb.write_txn(conn):
        conn.execute(_INSERT_SQL, row)
    record = get_record(conn, task_id) or {}
    return {"ok": True, "task_id": task_id, **record}


def pending_task_ids(conn: sqlite3.Connection, *, limit: int = SWEEP_LIMIT) -> list[str]:
    """Done tasks needing a generation pass, newest first (spec §2.1 SELECT).

    Three populations: never-generated, the fallback rows the sweep must retry, and rows
    that recorded an error with attempts left. A row that exhausted ``MAX_ATTEMPTS`` without
    error (a fixed fallback) is left alone — it is still listed and readable, just not
    retried forever.
    """
    rows = conn.execute(
        """
        SELECT t.id
          FROM tasks t
          LEFT JOIN task_done_summaries s ON s.task_id = t.id
         WHERE t.status = 'done' AND t.completed_at IS NOT NULL
           AND (s.task_id IS NULL
                OR (s.last_error IS NOT NULL AND s.attempt_count < ?))
         ORDER BY t.completed_at DESC
         LIMIT ?
        """,
        (MAX_ATTEMPTS, int(limit)),
    ).fetchall()
    return [r[0] for r in rows]


def generate_pending(conn: Optional[sqlite3.Connection] = None, *, board: Optional[str] = None,
                     limit: int = SWEEP_LIMIT, generated_by: str = "sweep") -> dict:
    """The sweep body: backfill/repair up to ``limit`` done tasks. Never raises."""
    own_conn = conn is None
    c = conn or _hook_conn(board)
    try:
        ids = pending_task_ids(c, limit=limit)
        done = 0
        failed = 0
        results = []
        for task_id in ids:
            try:
                res = _generate_locked(c, task_id, board=board, force=False, generated_by=generated_by)
            except Exception as exc:  # pragma: no cover - defensive
                failed += 1
                log.debug("sweep generation failed for %s: %s", task_id, exc)
                continue
            if res.get("ok"):
                done += 1
            else:
                failed += 1
            results.append({"task_id": task_id, "ok": bool(res.get("ok")),
                            "summary_source": res.get("summary_source")})
        return {"ok": True, "board": board or kb.get_current_board(), "candidates": len(ids),
                "generated": done, "failed": failed, "results": results}
    except Exception as exc:
        log.warning("done-summary sweep failed: %s", exc)
        return {"ok": False, "board": board, "reason": type(exc).__name__,
                "candidates": 0, "generated": 0, "failed": 0, "results": []}
    finally:
        if own_conn:
            try:
                c.close()
            except Exception:  # pragma: no cover - defensive
                pass


def mark_manual(conn: sqlite3.Connection, task_id: str, summary: str, *,
                actor: str = "manual") -> dict:
    """Record a hand-written summary (``summary_source='manual'``); sweeps never overwrite it."""
    text = (summary or "").strip()[:SUMMARY_MAX_CHARS]
    if not text:
        raise ValueError("summary must not be blank")
    existing = get_record(conn, task_id)
    if not existing:
        raise ValueError(f"no done-summary record for {task_id}")
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_done_summaries SET summary = ?, summary_source = ?, generated_at = ?, "
            "generated_by = ?, last_error = NULL WHERE task_id = ?",
            (text, SUMMARY_SOURCE_MANUAL, now, actor, task_id),
        )
    return get_record(conn, task_id) or {}


# --- Read API (spec §5.1/§5.2, §5.5) ---------------------------------------

def list_done(*, board: Optional[str] = None, include_archived: bool = False,
              only_review: bool = False, limit: int = 50, offset: int = 0,
              conn: Optional[sqlite3.Connection] = None) -> dict:
    """The done listing the UI calls: summary + review flag + review reason per task.

    Response shape is exactly spec §5.1: ``{board, now, total, returned, items[]}`` ordered
    ``completed_at DESC``. A done task the sweep has not reached yet is still returned with
    ``summary_source='pending'`` and ``fallback=True`` — a card is never hidden while a
    summary is pending. Default excludes ``archive_state='archived'`` (the plug-in's own soft
    flag, *not* ``tasks.status``); ``GET /done?include_archived=true`` returns them.
    """
    own_conn = conn is None
    c = conn or _hook_conn(board)
    try:
        ensure_schema(c)
        where = ["t.status = 'done'"]
        params: list[Any] = []
        if not include_archived:
            where.append("(s.archive_state IS NULL OR s.archive_state != 'archived')")
        if only_review:
            where.append("s.review_flag = 1")
        clause = " AND ".join(where)

        total = c.execute(
            f"SELECT COUNT(*) FROM tasks t LEFT JOIN task_done_summaries s "
            f"ON s.task_id = t.id WHERE {clause}", params).fetchone()[0]

        rows = c.execute(
            f"""
            SELECT t.id AS task_id, t.title AS task_title, t.assignee AS task_assignee,
                   t.created_by AS task_created_by, t.completed_at AS task_completed_at,
                   t.started_at AS task_started_at, t.session_id AS task_session_id,
                   s.{_SELECT_COLUMNS.replace(', ', ', s.').replace('task_id,', 'task_id,')}
              FROM tasks t
              LEFT JOIN task_done_summaries s ON s.task_id = t.id
             WHERE {clause}
             ORDER BY t.completed_at DESC, t.id DESC
             LIMIT ? OFFSET ?
            """,
            (*params, max(1, int(limit)), max(0, int(offset))),
        ).fetchall()

        items = []
        for row in rows:
            items.append(_item_from_joined_row(c, row))
        return {"board": board or kb.get_current_board(), "now": int(time.time()),
                "total": int(total), "returned": len(items), "items": items}
    finally:
        if own_conn:
            try:
                c.close()
            except Exception:  # pragma: no cover - defensive
                pass


def _pending_item(c: sqlite3.Connection, task_id: str) -> dict:
    """Synthesised item for a done task with no digest row yet (never blank)."""
    task = kb.get_task(c, task_id)
    title = task.title if task else ""
    return {
        "task_id": task_id,
        "title": title,
        "assignee": task.assignee if task else None,
        "created_by": task.created_by if task else None,
        "completed_at": task.completed_at if task else None,
        "summary": _fallback_summary(title, "summary pending"),
        "summary_source": SUMMARY_SOURCE_PENDING,
        "artifacts": [],
        "artifact_count": 0,
        "review_flag": False,
        "review_reasons": [],
        "review_reason": None,
        "review_rule_version": REVIEW_RULE_VERSION,
        "archive_state": "active",
        "archived_at": None,
        "archived_by": None,
        "generated_at": None,
        "fallback": True,
        "pending": True,
    }


def _item_from_joined_row(c: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """One joined ``tasks LEFT JOIN task_done_summaries`` row -> an API item."""
    keys = set(row.keys())
    if "summary" not in keys or row["summary"] is None:
        return _pending_item(c, row["task_id"])
    record = {
        "task_id": row["task_id"],
        "title": row["title"],
        "assignee": row["assignee"],
        "completed_at": row["completed_at"],
        "summary": row["summary"],
        "summary_source": row["summary_source"],
        "artifacts": _json_list(row["artifacts"]),
        "artifact_count": int(row["artifact_count"] or 0),
        "review_flag": bool(row["review_flag"]),
        "review_reasons": _json_list(row["review_reasons"]),
        "review_reason": row["review_reason"],
        "review_rule_version": row["review_rule_version"],
        "archive_state": row["archive_state"],
        "archived_at": row["archived_at"],
        "archived_by": row["archived_by"],
        "generated_at": row["generated_at"],
        "fallback": row["summary_source"] in (SUMMARY_SOURCE_FALLBACK, SUMMARY_SOURCE_PENDING),
    }
    # Denormalised task fields win for identity; the joined row is the live one.
    record["created_by"] = row["task_created_by"]
    record["title"] = row["task_title"] or record["title"]
    record["assignee"] = row["task_assignee"]
    record["completed_at"] = row["task_completed_at"]
    return record


def _json_list(value: Any) -> list:
    try:
        parsed = json.loads(value or "[]")
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def get_done_detail(task_id: str, *, board: Optional[str] = None,
                    conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    """One item plus the detail fields the UI shows on expand (spec §5.2).

    ``None`` when the task id is neither a stored digest nor a done task — the caller
    answers 404. Archived records are returned here regardless of ``archive_state``.
    """
    own_conn = conn is None
    c = conn or _hook_conn(board)
    try:
        ensure_schema(c)
        record = get_record(c, task_id)
        task = kb.get_task(c, task_id)
        if record is None and (task is None or task.status != "done"):
            return None
        item = record or _pending_item(c, task_id)
        detail: dict = {**item, "prompt_version": PROMPT_VERSION}
        detail["body"] = task.body if task else None
        detail["result"] = task.result if task else None
        detail["status"] = task.status if task else None
        try:
            runs = kb.list_runs(c, task_id) or []
            detail["runs"] = [{"id": r.id, "status": r.status, "outcome": r.outcome,
                               "summary": r.summary, "ended_at": r.ended_at} for r in runs]
            detail["events_tail"] = [{"kind": e.kind, "created_at": e.created_at}
                                     for e in (kb.list_events(c, task_id) or [])][-20:]
            detail["attachments"] = [{"id": a.id, "filename": a.filename, "size": a.size}
                                     for a in (kb.list_attachments(c, task_id) or [])]
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("detail expansion failed for %s: %s", task_id, exc)
            detail.setdefault("runs", [])
            detail.setdefault("events_tail", [])
            detail.setdefault("attachments", [])
        try:
            links = c.execute(
                "SELECT parent_id, child_id FROM task_links WHERE parent_id = ? OR child_id = ?",
                (task_id, task_id)).fetchall()
            detail["parents"] = [_link_brief(c, r["parent_id"]) for r in links
                                 if r["parent_id"] != task_id]
            detail["children"] = [_link_brief(c, r["child_id"]) for r in links
                                  if r["child_id"] != task_id]
        except Exception:  # pragma: no cover - defensive
            detail.setdefault("parents", [])
            detail.setdefault("children", [])
        return detail
    finally:
        if own_conn:
            try:
                c.close()
            except Exception:  # pragma: no cover - defensive
                pass


def _link_brief(conn: sqlite3.Connection, task_id: str) -> dict:
    task = kb.get_task(conn, task_id)
    return {"id": task_id, "title": task.title if task else None,
            "status": task.status if task else None}


# --- Archive (spec §4) ------------------------------------------------------

def set_archive_state(conn: sqlite3.Connection, task_ids: Iterable[str], state: str, *,
                      actor: Optional[str] = None) -> dict:
    """Soft archive/unarchive for the done listing. Never touches ``tasks.status``.

    ``state`` is one of :data:`VALID_ARCHIVE_STATES`. ``actor`` is required and never
    defaulted (spec §5.3: a missing actor is a 400 at the route layer). Only done tasks are
    eligible; anything else is reported in ``skipped`` rather than failing the batch.
    Unarchiving clears all four archive fields so a re-archive is clean and the timestamps
    never lie. Idempotent — a repeated call lands in ``already``.

    This is the plug-in's OWN flag. It is NOT ``kanban_db.archive_task()`` (that sets the
    terminal ``tasks.status='archived'``, kills a running worker and reaps the workspace) and
    it is never a delete (spec §4).
    """
    if state not in VALID_ARCHIVE_STATES:
        raise ValueError(f"state must be one of {VALID_ARCHIVE_STATES}")
    ids = [str(t).strip() for t in (task_ids or []) if str(t).strip()]
    if not ids:
        raise ValueError("no task_ids supplied")
    if len(ids) > 100:
        raise ValueError("at most 100 task_ids per call")

    actor_text = (actor or "").strip()[:200]
    if state in ("archived", "archive_requested") and not actor_text:
        raise ValueError("actor is required")

    now = int(time.time())
    changed: list[str] = []
    already: list[str] = []
    unknown: list[str] = []
    skipped: list[dict] = []

    with kb.write_txn(conn):
        for task_id in ids:
            task = kb.get_task(conn, task_id)
            if task is None:
                unknown.append(task_id)
                continue
            if task.status != "done":
                skipped.append({"task_id": task_id, "reason": "task is not done"})
                continue
            existing = get_record(conn, task_id)
            current = existing["archive_state"] if existing else "active"
            if current == state:
                already.append(task_id)
                continue
            if existing is None:
                # A done task with no digest yet: create the shell so the flag can live on
                # the row, then let the sweep fill in the real summary (spec §5.1 pending).
                conn.execute(
                    "INSERT INTO task_done_summaries (task_id, title, assignee, completed_at, "
                    "summary, summary_source, generated_at, generated_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(task_id) DO NOTHING",
                    (task_id, task.title or "", task.assignee, int(task.completed_at or now),
                     _fallback_summary(task.title, "summary pending"),
                     SUMMARY_SOURCE_PENDING, now, f"archive:{actor_text}"),
                )
            if state == "active":
                conn.execute(
                    "UPDATE task_done_summaries SET archive_state = 'active', "
                    "archive_requested_at = NULL, archive_requested_by = NULL, "
                    "archived_at = NULL, archived_by = NULL WHERE task_id = ?", (task_id,))
            elif state == "archive_requested":
                conn.execute(
                    "UPDATE task_done_summaries SET archive_state = 'archive_requested', "
                    "archive_requested_at = ?, archive_requested_by = ? WHERE task_id = ?",
                    (now, actor_text, task_id))
            else:
                conn.execute(
                    "UPDATE task_done_summaries SET archive_state = 'archived', "
                    "archive_requested_at = COALESCE(archive_requested_at, ?), "
                    "archive_requested_by = COALESCE(archive_requested_by, ?), "
                    "archived_at = ?, archived_by = ? WHERE task_id = ?",
                    (now, actor_text, now, actor_text, task_id))
            changed.append(task_id)

    return {"ok": True, "state": state, "changed": changed, "already": already,
            "unknown": unknown, "skipped": skipped}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the plug-in table on a connection whose DB predates it (idempotent)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_done_summaries (
            task_id         TEXT PRIMARY KEY,
            title           TEXT NOT NULL,
            assignee        TEXT,
            completed_at    INTEGER NOT NULL,
            summary         TEXT NOT NULL,
            summary_source  TEXT NOT NULL DEFAULT 'llm',
            artifacts       TEXT NOT NULL DEFAULT '[]',
            artifact_count  INTEGER NOT NULL DEFAULT 0,
            review_flag     INTEGER NOT NULL DEFAULT 0,
            review_reasons  TEXT NOT NULL DEFAULT '[]',
            review_reason   TEXT,
            review_rule_version TEXT NOT NULL DEFAULT 'v1',
            archive_state   TEXT NOT NULL DEFAULT 'active',
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
        """
    )


# --- Reporting --------------------------------------------------------------

def summarize_result(result: dict) -> str:
    """One-line human string for CLI / hook logging."""
    if not result.get("ok"):
        return f"FAILED {result.get('task_id')}: {result.get('reason')}"
    flags = ",".join(result.get("review_reasons") or []) or "-"
    return (f"{result.get('task_id')} [{result.get('summary_source')}] "
            f"flag={'yes' if result.get('review_flag') else 'no'} rules={flags}")


def cli(argv: Optional[Iterable[str]] = None) -> str:
    """``done-summary backfill [--limit N] [--board SLUG]`` for the CLI/gateway."""
    args = list(argv or [])
    board: Optional[str] = None
    limit = SWEEP_LIMIT
    if "--board" in args:
        try:
            board = args[args.index("--board") + 1]
        except IndexError:
            return "usage: done-summary backfill [--limit N] [--board SLUG]"
    if "--limit" in args:
        try:
            limit = max(1, min(500, int(args[args.index("--limit") + 1])))
        except (IndexError, ValueError):
            return "usage: done-summary backfill [--limit N] [--board SLUG]"
    if not args or args[0] not in {"backfill", "status"}:
        return ("usage: /done-summary backfill [--limit N] [--board SLUG]\n"
                "       /done-summary status [--board SLUG]")
    conn = _hook_conn(board)
    try:
        ensure_schema(conn)
        if args[0] == "status":
            pending = len(pending_task_ids(conn, limit=10000))
            total = conn.execute("SELECT COUNT(*) FROM task_done_summaries").fetchone()[0]
            flagged = conn.execute(
                "SELECT COUNT(*) FROM task_done_summaries WHERE review_flag = 1").fetchone()[0]
            return (f"done-summaries: {total} stored, {flagged} flagged for review, "
                    f"{pending} pending")
        res = generate_pending(conn, board=board, limit=limit, generated_by="cli-backfill")
        return (f"done-summary backfill: {res['generated']} generated, {res['failed']} failed, "
                f"{res['candidates']} candidates")
    finally:
        conn.close()

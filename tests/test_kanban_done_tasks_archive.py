"""Done Tasks plug-in: the soft-archive state machine (spec §4) and its HTTP surface (§5).

The three acceptance cases this file exists to pin:

1. mark for archive -> the record leaves the **default** done listing;
2. un-archive -> it is back in the default listing;
3. an archived record is still retrievable by id, and nothing destructive happened to it.

Every test runs against a throwaway board (``HERMES_HOME`` -> ``tmp_path``), so the live
board is never touched.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import done_tasks_archive as dta

def _hermes_repo() -> Path:
    """The Hermes install whose ``hermes_cli`` package is under test.

    ``HERMES_AGENT_REPO`` wins; otherwise search up from this file and fall back to
    ``~/.hermes/hermes-agent``. Run ``./install.sh`` first so the plug-in half and the
    core modules are in place. (This repo is a *distribution*: the tests are run from a
    directory that is not the Hermes checkout, so the install is resolved, not assumed.)
    """
    env = os.environ.get("HERMES_AGENT_REPO")
    if env:
        return Path(env).expanduser().resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "hermes_cli" / "kanban_db.py").is_file():
            return parent
    fallback = Path.home() / ".hermes" / "hermes-agent"
    if (fallback / "hermes_cli" / "kanban_db.py").is_file():
        return fallback
    raise RuntimeError(
        "cannot find the Hermes install (no hermes_cli/kanban_db.py); "
        "set HERMES_AGENT_REPO=/path/to/hermes-agent")


_REPO = _hermes_repo()
# The plug-in ships in-repo at plugins/done-tasks/ (the sibling summary card created the
# package and reserved dashboard/ for this card). Load the router by path so the test does
# not depend on plug-in discovery, and do NOT re-derive it from Path.home() — the fixture
# monkeypatches that to tmp_path.
PLUGIN_FILE = _REPO / "plugins" / "done-tasks" / "dashboard" / "plugin_api.py"


def _load_plugin_router(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


KANBAN_PLUGIN_FILE = _REPO / "plugins" / "kanban" / "dashboard" / "plugin_api.py"


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    app = FastAPI()
    # Both routers: tasks are created through the kanban API (as the dashboard does), then
    # archived through this plug-in's API — so the test exercises the real pairing.
    app.include_router(
        _load_plugin_router("hermes_kanban_plugin_for_done_tasks_test", KANBAN_PLUGIN_FILE),
        prefix="/api/plugins/kanban",
    )
    app.include_router(
        _load_plugin_router("done_tasks_plugin_test", PLUGIN_FILE),
        prefix="/api/plugins/done-tasks",
    )
    return TestClient(app)


def _done_task(client, title, *, summary="Did the thing.", assignee="press"):
    """Create a task through the API and complete it, so it lands in the done lane."""
    task = client.post("/api/plugins/kanban/tasks", json={"title": title}).json()["task"]
    with kbc.connect() as conn:
        with kbc.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
                (dta._now(), task["id"]),
            )
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, summary, started_at, ended_at) "
                "VALUES (?, 'press', 'done', 'completed', ?, ?, ?)",
                (task["id"], summary, dta._now(), dta._now()),
            )
    # Materialise the summary row the way the sibling generator would.
    with kbc.connect() as conn:
        dta.set_archive_state(conn, [task["id"]], dta.ARCHIVE_ACTIVE, actor="seed@test")
    return task["id"]


def _ids(payload):
    return [i["task_id"] for i in payload["items"]]


# --- 1. data model -----------------------------------------------------------

def test_summary_table_exists_with_archive_columns():
    with kbc.connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(task_done_summaries)")}
    assert {
        "archive_state", "archive_requested_at", "archive_requested_by",
        "archived_at", "archived_by", "review_flag", "review_reasons", "summary",
    } <= cols


def test_fresh_db_and_rebuilt_legacy_db_both_hold_the_new_table(tmp_path, monkeypatch):
    """The spec's guard test: a `CREATE TABLE IF NOT EXISTS` in SCHEMA_SQL must reach both."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "h"))
    (tmp_path / "h").mkdir()
    path = kb.init_db()
    with kbc.connect(path) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_done_summaries'"
        ).fetchone() is not None


# --- 2. the required cycle: mark -> hidden; unarchive -> visible -------------

def test_mark_for_archive_hides_from_default_list_and_unarchive_restores(client):
    keep = _done_task(client, "Keep me visible")
    file_it = _done_task(client, "File this one")

    before = client.get("/api/plugins/done-tasks/done").json()
    assert set(_ids(before)) == {keep, file_it}

    r = client.post(
        "/api/plugins/done-tasks/archive",
        json={"task_ids": [file_it], "actor": "chad.d@osyoossigns.ca"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["archived"] == [file_it], body

    # Hidden from the everyday view...
    after = client.get("/api/plugins/done-tasks/done").json()
    assert _ids(after) == [keep]
    assert after["total"] == 1

    # ...but present when explicitly requested.
    with_archived = client.get("/api/plugins/done-tasks/done?include_archived=true").json()
    assert set(_ids(with_archived)) == {keep, file_it}

    # Un-archive restores it.
    r = client.post(
        "/api/plugins/done-tasks/unarchive",
        json={"task_ids": [file_it], "actor": "chad.d@osyoossigns.ca"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["unarchived"] == [file_it]
    assert set(_ids(client.get("/api/plugins/done-tasks/done").json())) == {keep, file_it}


def test_request_is_promoted_to_archived_in_one_write(client):
    """`archive_requested` is schema-legal but never a resting state (§4)."""
    tid = _done_task(client, "Promote me")
    client.post("/api/plugins/done-tasks/archive", json={"task_ids": [tid], "actor": "a@b"})

    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT archive_state, archived_at, archived_by, archive_requested_at, "
            "archive_requested_by FROM task_done_summaries WHERE task_id=?", (tid,)
        ).fetchone()
    assert row["archive_state"] == dta.ARCHIVE_ARCHIVED
    assert row["archived_at"] is not None and row["archived_by"] == "a@b"
    # The original tap is preserved, not overwritten by the promotion.
    assert row["archive_requested_at"] is not None and row["archive_requested_by"] == "a@b"

    detail = client.get(f"/api/plugins/done-tasks/done/{tid}").json()
    assert detail["archive_state"] == dta.ARCHIVE_ARCHIVED


# --- 3. archived records stay retrievable, and nothing destructive happens ----

def test_archived_record_is_still_retrievable_by_id(client):
    tid = _done_task(client, "Retrievable after archiving", summary="Replaced the roller.")
    client.post("/api/plugins/done-tasks/archive", json={"task_ids": [tid], "actor": "a@b"})

    r = client.get(f"/api/plugins/done-tasks/done/{tid}")
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["task_id"] == tid
    assert detail["archive_state"] == dta.ARCHIVE_ARCHIVED
    assert detail["summary"] == "Replaced the roller."
    # The detail view carries the live task, its run history and its prompts.
    assert detail["status"] == "done"
    assert len(detail["runs"]) == 1
    assert detail["prompt_version"] == "v1"


def test_archive_never_touches_task_status_or_deletes_anything(client):
    tid = _done_task(client, "Nothing destructive please")

    with kbc.connect() as conn:
        before_task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        before_runs = conn.execute("SELECT COUNT(*) c FROM task_runs WHERE task_id=?", (tid,)).fetchone()["c"]
        before_events = conn.execute("SELECT COUNT(*) c FROM task_events WHERE task_id=?", (tid,)).fetchone()["c"]

    client.post("/api/plugins/done-tasks/archive", json={"task_ids": [tid], "actor": "a@b"})
    client.post("/api/plugins/done-tasks/unarchive", json={"task_ids": [tid], "actor": "a@b"})
    client.post("/api/plugins/done-tasks/archive", json={"task_ids": [tid], "actor": "a@b"})

    with kbc.connect() as conn:
        after_task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        after_runs = conn.execute("SELECT COUNT(*) c FROM task_runs WHERE task_id=?", (tid,)).fetchone()["c"]
        after_events = conn.execute("SELECT COUNT(*) c FROM task_events WHERE task_id=?", (tid,)).fetchone()["c"]
        rows = conn.execute(
            "SELECT COUNT(*) c FROM task_done_summaries WHERE task_id=?", (tid,)
        ).fetchone()["c"]

    assert after_task == before_task, "the summary feature must not touch the tasks row"
    assert after_task["status"] == "done"
    assert after_runs == before_runs
    assert after_events == before_events
    assert rows == 1, "the record is never duplicated or removed"


def test_archive_is_not_the_board_status_archive(client):
    """Never `tasks.status='archived'` — that verb kills workers and reaps workspaces."""
    tid = _done_task(client, "Soft only")
    client.post("/api/plugins/done-tasks/archive", json={"task_ids": [tid], "actor": "a@b"})
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "done"


# --- 4. reversal clears all four fields ---------------------------------------

def test_unarchive_clears_all_four_archive_fields(client):
    tid = _done_task(client, "Clean reversal")
    client.post("/api/plugins/done-tasks/archive", json={"task_ids": [tid], "actor": "first@b"})
    client.post("/api/plugins/done-tasks/unarchive", json={"task_ids": [tid], "actor": "second@b"})

    with kbc.connect() as conn:
        row = conn.execute("SELECT * FROM task_done_summaries WHERE task_id=?", (tid,)).fetchone()
    assert row["archive_state"] == dta.ARCHIVE_ACTIVE
    assert row["archived_at"] is None and row["archived_by"] is None
    assert row["archive_requested_at"] is None and row["archive_requested_by"] is None


def test_rearchive_after_unarchive_records_the_second_actor(client):
    tid = _done_task(client, "Re-archive with a fresh stamp")
    client.post("/api/plugins/done-tasks/archive", json={"task_ids": [tid], "actor": "first@b"})
    client.post("/api/plugins/done-tasks/unarchive", json={"task_ids": [tid], "actor": "first@b"})
    client.post("/api/plugins/done-tasks/archive", json={"task_ids": [tid], "actor": "second@b"})

    with kbc.connect() as conn:
        row = conn.execute("SELECT * FROM task_done_summaries WHERE task_id=?", (tid,)).fetchone()
    assert row["archived_by"] == "second@b", "the timestamps must never lie about who filed it"


# --- 5. batching / idempotence / refusals -------------------------------------

def test_batch_archive_returns_already_unknown_and_skipped(client):
    a = _done_task(client, "Batch a")
    b = _done_task(client, "Batch b")
    not_done = client.post("/api/plugins/kanban/tasks", json={"title": "still open"}).json()["task"]["id"]

    r = client.post(
        "/api/plugins/done-tasks/archive",
        json={"task_ids": [a, b, not_done, "t_nosuch"], "actor": "chad@b"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["archived"]) == sorted([a, b])
    assert body["unknown"] == ["t_nosuch"]
    assert body["skipped"] == [{"task_id": not_done, "reason": "task is not done"}]
    assert body["already"] == []

    # Second call: both now report as already archived, and ok stays true.
    again = client.post(
        "/api/plugins/done-tasks/archive", json={"task_ids": [a, b], "actor": "chad@b"}
    ).json()
    assert again["ok"] is True
    assert sorted(again["already"]) == sorted([a, b])
    assert again["archived"] == []


def test_missing_actor_is_a_400_and_not_defaulted(client):
    tid = _done_task(client, "Needs an actor")
    for body in ({"task_ids": [tid]}, {"task_ids": [tid], "actor": "   "}):
        r = client.post("/api/plugins/done-tasks/archive", json=body)
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["reason"] == "actor is required"
    # And nothing was written.
    assert client.get(f"/api/plugins/done-tasks/done/{tid}").json()["archive_state"] == "active"


def test_missing_or_empty_task_ids_is_a_400(client):
    for body in ({"actor": "a@b"}, {"task_ids": [], "actor": "a@b"}, {"task_ids": ["  "], "actor": "a@b"}):
        r = client.post("/api/plugins/done-tasks/archive", json=body)
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["reason"] == "no task_ids supplied"


def test_batch_is_capped_at_100_ids(client):
    r = client.post(
        "/api/plugins/done-tasks/archive",
        json={"task_ids": [f"t_{i:08d}" for i in range(101)], "actor": "a@b"},
    )
    assert r.status_code == 400, r.text
    assert "too many task_ids" in r.json()["detail"]["reason"]


def test_unarchive_on_an_active_record_is_idempotent(client):
    tid = _done_task(client, "Never archived")
    r = client.post("/api/plugins/done-tasks/unarchive", json={"task_ids": [tid], "actor": "a@b"})
    assert r.status_code == 200, r.text
    assert r.json()["already"] == [tid]
    assert tid in _ids(client.get("/api/plugins/done-tasks/done").json())


def test_archive_of_an_existing_active_row_is_not_reported_as_already(client):
    """`already` must mean "stored state == target", not "a row exists".

    Regression guard: the first cut treated a row-less task as `already`, so a done task the
    sweep had not reached was never written and stayed in the default listing.
    """
    tid = _done_task(client, "Has a row, still active")
    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT archive_state FROM task_done_summaries WHERE task_id=?", (tid,)
        ).fetchone()
    assert row["archive_state"] == dta.ARCHIVE_ACTIVE

    body = client.post("/api/plugins/done-tasks/archive", json={"task_ids": [tid], "actor": "a@b"}).json()
    assert body["archived"] == [tid], body
    assert body["already"] == []
    assert tid not in _ids(client.get("/api/plugins/done-tasks/done").json())

    # Now it genuinely is already archived.
    again = client.post("/api/plugins/done-tasks/archive", json={"task_ids": [tid], "actor": "a@b"}).json()
    assert again["already"] == [tid] and again["archived"] == []


def test_archive_state_values_match_the_sibling_module(client):
    """One shared vocabulary: the plug-in's enum, the sibling's, and the spec must agree."""
    from hermes_cli import done_tasks_summary as dts
    assert set(dta.ARCHIVE_STATES) == {"active", "archive_requested", "archived"}
    for name in ("ARCHIVE_ACTIVE", "ARCHIVE_REQUESTED", "ARCHIVE_ARCHIVED"):
        if hasattr(dts, name):
            assert getattr(dts, name) == getattr(dta, name), name
    # And the enum never collides with the board's own terminal status.
    assert dta.ARCHIVE_ARCHIVED in kb.VALID_STATUSES
    assert dta.ARCHIVE_ACTIVE not in kb.VALID_STATUSES
    assert dta.ARCHIVE_REQUESTED not in kb.VALID_STATUSES


# --- 6. view options ----------------------------------------------------------

def test_only_review_filters_and_archive_state_changes_neither(client):
    quiet = _done_task(client, "Quiet one")
    with kbc.connect() as conn:
        with kbc.write_txn(conn):
            conn.execute(
                "UPDATE task_done_summaries SET review_flag=1, review_reasons='[\"R2\"]', "
                "review_reason='Touches money or pricing.' WHERE task_id=?", (quiet,)
            )
    flagged = _done_task(client, "Flagged one")
    with kbc.connect() as conn:
        with kbc.write_txn(conn):
            conn.execute(
                "UPDATE task_done_summaries SET review_flag=1, review_reasons='[\"R2\",\"R5\"]', "
                "review_reason='Touches money or pricing.' WHERE task_id=?", (flagged,)
            )

    only = client.get("/api/plugins/done-tasks/done?only_review=true").json()
    assert set(_ids(only)) == {quiet, flagged}
    item = next(i for i in only["items"] if i["task_id"] == flagged)
    assert item["review_flag"] is True
    assert item["review_reasons"] == ["R2", "R5"]
    assert item["review_reason"] == "Touches money or pricing."

    # Archiving a flagged card hides it from the default view but not from only_review+archived.
    client.post("/api/plugins/done-tasks/archive", json={"task_ids": [flagged], "actor": "a@b"})
    assert flagged not in _ids(client.get("/api/plugins/done-tasks/done?only_review=true").json())
    assert flagged in _ids(
        client.get("/api/plugins/done-tasks/done?only_review=true&include_archived=true").json()
    )


def test_unknown_task_detail_is_404(client):
    r = client.get("/api/plugins/done-tasks/done/t_nosuch")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


def test_validation_error_paths_do_not_raise_500(client):
    """§5.3/§5.4 precedent: bad input is a 4xx or an ok:false, never a stack trace."""
    assert client.post("/api/plugins/done-tasks/archive", json={"task_ids": [], "actor": "a@b"}).status_code == 400
    r = client.post("/api/plugins/done-tasks/regenerate/t_nosuch", json={})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["reason"] == "task not found"


# --- 7. sibling-generator boundary --------------------------------------------

def test_pending_row_is_materialised_when_archiving_existing_data(client):
    """A done task the summary sweep has not reached is still archivable (§5.1)."""
    tid = _done_task(client, "Never swept yet")
    with kbc.connect() as conn:
        with kbc.write_txn(conn):
            conn.execute("DELETE FROM task_done_summaries WHERE task_id=?", (tid,))
        # The task is still done; only its summary row is gone.
        assert kb.get_task(conn, tid).status == "done"
        assert dta.get_done(conn, tid).summary_source == dta.SOURCE_PENDING

    r = client.post("/api/plugins/done-tasks/archive", json={"task_ids": [tid], "actor": "a@b"})
    assert r.status_code == 200, r.text
    assert r.json()["archived"] == [tid]

    with kbc.connect() as conn:
        row = conn.execute("SELECT * FROM task_done_summaries WHERE task_id=?", (tid,)).fetchone()
    assert row["archive_state"] == dta.ARCHIVE_ARCHIVED
    assert (row["summary"] or "").strip(), "a materialised row is never blank"
    assert row["summary_source"] == dta.SOURCE_PENDING
    assert row["last_error"] is None, "the sweep must still be willing to adopt this row"
    assert tid not in _ids(client.get("/api/plugins/done-tasks/done").json())


def test_pending_done_task_carries_no_lifetime_review_flag(client):
    """A pending row is NOT flagged for review — otherwise every fresh completion shows a badge."""
    tid = _done_task(client, "Pending badge check")
    with kbc.connect() as conn:
        with kbc.write_txn(conn):
            conn.execute("DELETE FROM task_done_summaries WHERE task_id=?", (tid,))
        pending = dta.get_done(conn, tid)
    assert pending.review_flag is False
    assert pending.review_reasons == []
    assert pending.fallback is True and pending.pending is True


def test_done_task_with_no_summary_row_is_still_listed(client):
    """§5.1: never hide a card just because the sweep has not reached it."""
    tid = _done_task(client, "Not yet swept")
    with kbc.connect() as conn:
        with kbc.write_txn(conn):
            conn.execute("DELETE FROM task_done_summaries WHERE task_id=?", (tid,))
    payload = client.get("/api/plugins/done-tasks/done").json()
    assert tid in _ids(payload)
    item = next(i for i in payload["items"] if i["task_id"] == tid)
    assert item["summary_source"] == dta.SOURCE_PENDING
    assert item["fallback"] is True


def test_regenerate_preserves_archive_state(client):
    """A rules-version bump must be able to regenerate without losing the archive flag (§3)."""
    tid = _done_task(client, "Regenerate keeps my filing")
    client.post("/api/plugins/done-tasks/archive", json={"task_ids": [tid], "actor": "a@b"})
    r = client.post(f"/api/plugins/done-tasks/regenerate/{tid}", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    # Whatever the sibling generator does, the archive flag survives.
    assert body.get("archive_state", dta.ARCHIVE_ARCHIVED) == dta.ARCHIVE_ARCHIVED
    with kbc.connect() as conn:
        row = conn.execute("SELECT archive_state FROM task_done_summaries WHERE task_id=?", (tid,)).fetchone()
    assert row["archive_state"] == dta.ARCHIVE_ARCHIVED


# --- 8. list shape ------------------------------------------------------------

def test_listing_is_newest_first_and_shape_is_stable(client):
    first = _done_task(client, "Older")
    second = _done_task(client, "Newer")
    with kbc.connect() as conn:
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET completed_at=1000 WHERE id=?", (first,))
            conn.execute("UPDATE tasks SET completed_at=2000 WHERE id=?", (second,))
    payload = client.get("/api/plugins/done-tasks/done").json()
    assert _ids(payload)[:2] == [second, first]

    item = payload["items"][0]
    assert set(item) == {
        "task_id", "title", "assignee", "created_by", "completed_at", "summary",
        "summary_source", "artifacts", "artifact_count", "review_flag", "review_reasons",
        "review_reason", "review_rule_version", "review_priority", "archive_state",
        "archive_requested_at", "archive_requested_by", "archived_at", "archived_by",
        "generated_at", "fallback", "pending",
    }
    assert payload["returned"] == len(payload["items"])
    assert payload["total"] == 2


def test_board_param_validation(client):
    assert client.get("/api/plugins/done-tasks/done?board=No Such Board!!").status_code == 400
    assert client.get("/api/plugins/done-tasks/done?board=nope-board").status_code == 404
    assert client.get("/api/plugins/done-tasks/done?board=default").status_code == 200

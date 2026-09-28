"""Tests for the Done Tasks plug-in backend: summary generation + review flag.

Covers the acceptance criteria of card ``t_94fd868b`` / spec ``DONE-TASKS-PLUGIN-SPEC.md``:

* completing a task produces a stored summary record (through the real completion path)
* every deterministic rule R1-R9 has a positive AND a negative case
* a failed summarisation still yields a usable, regenerable record
* the read API returns the documented response shape

Rule tests drive :func:`evaluate_review_rules` directly (it is a pure function, so the test
table is the specification); storage tests go through ``kanban_db`` so the trigger, the
upsert and the status guard are exercised for real.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import done_tasks_summary as dts
from hermes_cli.done_tasks_summary import DoneTaskContext as C


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated board.

    ``HERMES_KANBAN_DB`` / ``HERMES_KANBAN_HOME`` are exported into worker shells and
    ``kanban_db_path()`` prefers them over ``HERMES_HOME``, so both must be dropped or a
    test silently writes to the live board at ``/home/hermes/.hermes/kanban.db``.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    resolved = kb.kanban_db_path()
    assert str(tmp_path) in str(resolved), (
        f"board isolation failed: writes would land in {resolved}")
    return home


@pytest.fixture
def conn(kanban_home):
    c = kbc.connect()
    yield c
    c.close()


@pytest.fixture
def fake_llm(monkeypatch):
    """Deterministic summariser; returns the patched callable so tests can re-patch it."""
    def _install(summary="Did the task and wrote up the result.", hint=None):
        def _call(ctx, task_id):
            return {"summary": summary, "review_hint": hint}, None
        monkeypatch.setattr(dts, "_call_summary_llm", _call)
        return _call
    return _install


def _complete(conn, *, title="Do a thing", body="x", assignee="worker", created_by="chad.d@x",
              summary="Did it.", metadata=None, claimer="worker", generate=True):
    """Complete a task for real; ``generate`` runs the pass the completion hook runs."""
    task_id = kb.create_task(conn, title=title, body=body, assignee=assignee,
                             created_by=created_by)
    kb.claim_task(conn, task_id, claimer=claimer)
    kb.complete_task(conn, task_id, summary=summary, metadata=metadata)
    if generate:
        dts.generate_done_summary(conn, task_id, generated_by="test")
    return task_id


# ---------------------------------------------------------------------------
# Storage: completion produces a record
# ---------------------------------------------------------------------------

def test_completing_a_task_produces_a_stored_record(conn, fake_llm):
    fake_llm()
    task_id = _complete(conn, metadata={"artifacts": ["/tmp/out.md"]})

    record = dts.get_record(conn, task_id)
    assert record is not None, "completion must produce a stored digest"
    assert record["task_id"] == task_id
    assert record["summary"] == "Did the task and wrote up the result."
    assert record["summary_source"] == dts.SUMMARY_SOURCE_LLM
    assert record["artifacts"] == ["/tmp/out.md"]
    assert record["artifact_count"] == 1
    assert record["review_rule_version"] == dts.REVIEW_RULE_VERSION
    assert record["archive_state"] == "active"
    assert record["last_error"] is None


def test_generation_is_idempotent_and_bumps_attempts(conn, fake_llm):
    fake_llm()
    task_id = _complete(conn)

    dts.generate_done_summary(conn, task_id, generated_by="a")
    dts.generate_done_summary(conn, task_id, generated_by="b")

    rows = conn.execute("SELECT COUNT(*) FROM task_done_summaries WHERE task_id = ?",
                        (task_id,)).fetchone()[0]
    assert rows == 1, "the hook and the sweep race; the row must never duplicate"
    record = dts.get_record(conn, task_id)
    # one from _complete + the two explicit passes above
    assert record["attempt_count"] == 3


def test_generation_refuses_a_task_that_is_not_done(conn, fake_llm):
    fake_llm()
    task_id = kb.create_task(conn, title="Not finished", assignee="a", created_by="b")
    result = dts.generate_done_summary(conn, task_id)
    assert result["ok"] is False
    assert result["skipped"] is True
    assert dts.get_record(conn, task_id) is None


def test_unknown_task_is_reported_not_raised(conn, fake_llm):
    fake_llm()
    result = dts.generate_done_summary(conn, "t_nope")
    assert result == {"ok": False, "task_id": "t_nope", "reason": "unknown task"}


def test_hook_entry_point_never_raises(conn, fake_llm, monkeypatch):
    """The completion hook must never break a transition (spec §5.5)."""
    fake_llm()
    task_id = _complete(conn)

    def _explode(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(dts, "_generate_locked", _explode)
    result = dts.generate_done_summary(conn, task_id)
    assert result["ok"] is False
    assert result["reason"] == "RuntimeError"


# ---------------------------------------------------------------------------
# Review rules R1-R9: one positive + one negative each
# ---------------------------------------------------------------------------

#: (rule id, positive context, negative context). ``artifacts`` are supplied on every
#: context except where R4 itself is under test, so R4 never masks the rule being checked.
RULE_CASES = [
    (
        "R1",
        C(artifacts=["/a.md"], started_at=1000, completed_at=1000 + 3601),
        C(artifacts=["/a.md"], started_at=1000, completed_at=1000 + 3599),
    ),
    (
        "R1",  # parent of a fan-out
        C(artifacts=["/a.md"], children_count=3),
        C(artifacts=["/a.md"], children_count=2),
    ),
    (
        "R2",
        C(artifacts=["/a.md"], title="Fix the invoice total"),
        C(artifacts=["/a.md"], title="Fix the header spacing"),
    ),
    (
        "R2",  # a literal dollar amount also counts
        C(artifacts=["/a.md"], body="the balance is $ 250"),
        C(artifacts=["/a.md"], body="the balance is fine"),
    ),
    (
        "R3",
        C(artifacts=["/a.md"], body="edit config.yaml for the gateway"),
        C(artifacts=["/a.md"], body="write a report about printers"),
    ),
    (
        "R3",  # credential material in a path
        C(artifacts=["/home/hermes/.env"]),
        C(artifacts=["/home/hermes/notes.md"]),
    ),
    (
        "R4",
        C(title="Read the spec", body="then report back"),
        C(title="Read the spec", body="then report back", attachment_count=1),
    ),
    (
        "R4",  # an explicit "nothing to build" opt-out suppresses R4
        C(title="Read the spec", body="then report back"),
        C(title="Read the spec", body="this is a self-test, nothing to build"),
    ),
    (
        "R5",
        C(artifacts=["/a.md"], attempt_index=2),
        C(artifacts=["/a.md"], attempt_index=1, runs=[{"status": "running", "outcome": None}]),
    ),
    (
        "R5",  # a crashed run
        C(artifacts=["/a.md"], runs=[{"status": "crashed", "outcome": None}]),
        C(artifacts=["/a.md"], runs=[{"status": "running", "outcome": "completed"}]),
    ),
    (
        "R5",  # archived then completed again
        C(artifacts=["/a.md"], events=["created", "archived", "completed"]),
        C(artifacts=["/a.md"], events=["created", "completed", "archived"]),
    ),
    (
        "R6",
        C(artifacts=["/a.md"], title="Purge the old logs"),
        C(artifacts=["/a.md"], title="Write summary cards"),
    ),
    (
        "R6",  # "rotate" as a whole word
        C(artifacts=["/a.md"], body="rotate the relay key"),
        C(artifacts=["/a.md"], body="the rotary cutter is blunt"),
    ),
    (
        "R7",
        C(artifacts=["/a.md"], assignee="default",
          changed_files=["/home/hermes/.hermes/profiles/daemon/skills/x.md"]),
        C(artifacts=["/a.md"], assignee="daemon",
          changed_files=["/home/hermes/.hermes/profiles/daemon/skills/x.md"]),
    ),
    (
        "R8",
        C(artifacts=["/a.md"], metadata={"review_hint": "Have a look at the wording."}),
        C(artifacts=["/a.md"], metadata={"review_hint": None}),
    ),
    (
        "R9",
        C(artifacts=["/srv/other/out.md"], session_id="s1", workspaces_root="/home/hermes/ws"),
        C(artifacts=["/home/hermes/ws/t_1/out.md"], session_id="s1", workspaces_root="/home/hermes/ws"),
    ),
]


@pytest.mark.parametrize("rule_id,positive,negative",
                         RULE_CASES, ids=[f"{r[0]}-{i}" for i, r in enumerate(RULE_CASES)])
def test_rule_has_positive_and_negative_case(rule_id, positive, negative):
    flag, reasons, reason = dts.evaluate_review_rules(positive)
    assert flag is True, f"{rule_id} must fire on its positive case"
    assert rule_id in reasons
    assert reason

    flag, reasons, reason = dts.evaluate_review_rules(negative)
    assert rule_id not in reasons, f"{rule_id} must not fire on its negative case"


def test_every_spec_rule_is_covered_by_the_table():
    covered = {case[0] for case in RULE_CASES}
    assert covered == set(dts.RULE_ORDER), "every rule needs a case, and no case may be stray"


def test_no_rules_fire_on_a_plain_read_only_investigation():
    ctx = C(title="Investigate the slow printer", body="Read the logs and write a report.",
            created_by="chad.d@x", assignee="gnosis",
            summary="Found the cause and wrote it up.",
            artifacts=["/home/hermes/report.md"], attachment_count=1,
            started_at=1000, completed_at=1000 + 60,
            runs=[{"status": "running", "outcome": "completed"}],
            events=["created", "claimed", "completed"])
    flag, reasons, reason = dts.evaluate_review_rules(ctx)
    assert flag is False
    assert reasons == []
    assert reason is None


# ---------------------------------------------------------------------------
# Rule engine safety invariants (spec §3)
# ---------------------------------------------------------------------------

def test_rule_engine_is_deterministic():
    ctx = C(title="Invoice fix", body="price", artifacts=[])
    assert dts.evaluate_review_rules(ctx) == dts.evaluate_review_rules(ctx)


def test_flag_and_reasons_are_mutually_implying():
    for _, positive, negative in RULE_CASES:
        for ctx in (positive, negative):
            flag, reasons, _ = dts.evaluate_review_rules(ctx)
            assert bool(reasons) == flag


def test_reasons_are_ordered_by_rule_priority():
    ctx = C(title="Purge the invoice", body="delete it", artifacts=[])
    _, reasons, reason = dts.evaluate_review_rules(ctx)
    assert reasons == sorted(reasons, key=dts.RULE_ORDER.index)
    assert reason == dts.RULE_SENTENCES[reasons[0]]


def test_llm_hint_cannot_clear_a_deterministic_hit():
    ctx = C(title="Fix the invoice", body="money", artifacts=["/a.md"],
            metadata={"review_hint": None})
    flag, reasons, reason = dts.evaluate_review_rules(ctx)
    assert flag is True
    assert reasons == ["R2"]
    assert reason == "Touches money or pricing."


def test_hint_sentence_is_used_when_it_is_the_only_reason():
    ctx = C(title="Tidy the notes", artifacts=["/a.md"],
            metadata={"review_hint": "Give this a quick read."})
    flag, reasons, reason = dts.evaluate_review_rules(ctx)
    assert flag is True
    assert reasons == ["R8"]
    assert reason == "Give this a quick read."


def test_review_reason_is_capped_and_redacted():
    long_hint = "Please check " + ("x" * 400) + " api_key=sk-abcdef123456789"
    ctx = C(title="Tidy", artifacts=["/a.md"], metadata={"review_hint": long_hint})
    _, reasons, reason = dts.evaluate_review_rules(ctx)
    assert reasons == ["R8"]
    assert len(reason) <= dts.REVIEW_REASON_MAX_CHARS
    assert "sk-abcdef123456789" not in reason


def test_destructive_keyword_matching_ignores_substrings():
    _, reasons, _ = dts.evaluate_review_rules(C(title="the rotary cutter needs a new blade"))
    assert "R6" not in reasons, "'rotate' must match as a word, not inside 'rotary'"


# ---------------------------------------------------------------------------
# Artifact extraction (spec §2.4)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (["/a.md", "/b.md"], ["/a.md", "/b.md"]),
    ('["/a.md", "/b.md"]', ["/a.md", "/b.md"]),
    ("['/a.md', '/b.md']", ["/a.md", "/b.md"]),          # the stringified Python list seen live
    ("/a.md\n/b.md", ["/a.md", "/b.md"]),
    (None, []),
    ("", []),
    (123, []),
])
def test_coerce_path_list_tolerates_every_shape_found_in_the_wild(raw, expected):
    assert dts._coerce_path_list(raw) == expected


def test_extract_artifacts_unions_and_dedupes(conn, kanban_home):
    task_id = _complete(conn, metadata={"artifacts": ["/a.md", "/b.md"]})
    stashed = kanban_home / "kanban" / "attachments" / task_id
    stashed.mkdir(parents=True, exist_ok=True)
    (stashed / "c.md").write_text("x")
    kb.add_attachment(conn, task_id, filename="c.md", stored_path=str(stashed / "c.md"),
                      size=1, uploaded_by="test")
    paths = dts.extract_artifacts(conn, task_id)
    assert "/a.md" in paths and "/b.md" in paths
    assert str(stashed / "c.md") in paths
    assert len(paths) == len(set(paths))


def test_extract_artifacts_never_raises_on_a_bad_blob():
    class _Run:
        metadata = {"artifacts": "{not: valid}"}
    assert dts._coerce_path_list(_Run.metadata["artifacts"]) == ["{not: valid}"]


# ---------------------------------------------------------------------------
# Degradation ladder (spec §2.3)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("error_name", ["RuntimeError", "parse_failed", "empty_summary",
                                        "auxiliary_client_unavailable"])
def test_a_failed_summarisation_still_yields_a_usable_record(conn, monkeypatch, error_name):
    task_id = _complete(conn, title="Reconcile the bank feed")
    monkeypatch.setattr(dts, "_call_summary_llm", lambda ctx, tid: (None, error_name))

    result = dts.generate_done_summary(conn, task_id)
    assert result["ok"] is True, "a summary failure must not fail the record"

    record = dts.get_record(conn, task_id)
    assert record["summary_source"] == dts.SUMMARY_SOURCE_FALLBACK
    assert record["last_error"] == error_name
    assert record["summary"], "the fallback summary must never be blank"
    assert "Reconcile the bank feed" in record["summary"]
    assert record["fallback"] is True


def test_short_model_reply_is_treated_as_empty(conn, monkeypatch):
    task_id = _complete(conn)
    monkeypatch.setattr(dts, "_call_summary_llm",
                        lambda ctx, tid: ({"summary": "ok"}, None))
    result = dts.generate_done_summary(conn, task_id)
    assert result["summary_source"] == dts.SUMMARY_SOURCE_FALLBACK
    assert result["last_error"] == "empty_summary"


def test_a_usable_summary_clears_a_previous_error(conn, monkeypatch):
    task_id = _complete(conn)
    monkeypatch.setattr(dts, "_call_summary_llm", lambda ctx, tid: (None, "parse_failed"))
    dts.generate_done_summary(conn, task_id)
    assert dts.get_record(conn, task_id)["last_error"] == "parse_failed"

    monkeypatch.setattr(dts, "_call_summary_llm",
                        lambda ctx, tid: ({"summary": "Now it works properly."}, None))
    record = dts.generate_done_summary(conn, task_id)
    assert record["summary_source"] == dts.SUMMARY_SOURCE_LLM
    assert record["last_error"] is None


def test_an_llm_reply_that_raises_inside_the_hook_degrades_instead(conn, monkeypatch):
    """Even an exception escaping the summariser must not break the completion path."""
    task_id = _complete(conn)

    class _Boom(Exception):
        pass

    def _raise(*a, **kw):
        raise _Boom()

    monkeypatch.setattr(dts, "_call_summary_llm_inner", _raise)
    record = dts.generate_done_summary(conn, task_id)
    assert record["ok"] is True
    assert record["summary_source"] == dts.SUMMARY_SOURCE_FALLBACK


def test_fallback_rows_are_retried_until_the_attempt_cap(conn, monkeypatch):
    task_id = _complete(conn)
    monkeypatch.setattr(dts, "_call_summary_llm", lambda ctx, tid: (None, "RuntimeError"))

    dts.generate_done_summary(conn, task_id)
    assert task_id in dts.pending_task_ids(conn), "a fallback row must be retried"

    for _ in range(dts.MAX_ATTEMPTS):
        dts.generate_pending(conn)

    record = dts.get_record(conn, task_id)
    assert record["attempt_count"] >= dts.MAX_ATTEMPTS
    assert record["summary"], "the exhausted row is still listed and still readable"
    assert task_id not in dts.pending_task_ids(conn), "retries must be bounded"


def test_sweep_backfills_a_task_that_was_done_before_the_plugin_existed(conn, fake_llm):
    """The 78 already-done tasks: no row at all, the sweep must create one (spec §2.1)."""
    fake_llm()
    old = _complete(conn, title="A task completed before the plug-in shipped")
    conn.execute("DELETE FROM task_done_summaries WHERE task_id = ?", (old,))
    conn.commit()
    assert dts.get_record(conn, old) is None

    result = dts.generate_pending(conn)
    assert result["generated"] >= 1
    record = dts.get_record(conn, old)
    assert record is not None
    assert record["summary_source"] == dts.SUMMARY_SOURCE_LLM


def test_manual_summary_is_never_overwritten_by_a_sweep(conn, fake_llm):
    fake_llm()
    task_id = _complete(conn)
    dts.mark_manual(conn, task_id, "Chad rewrote this by hand.", actor="chad.d@x")

    dts.generate_done_summary(conn, task_id)          # sweep-style, no force
    assert dts.get_record(conn, task_id)["summary_source"] == dts.SUMMARY_SOURCE_MANUAL
    assert dts.get_record(conn, task_id)["summary"] == "Chad rewrote this by hand."

    dts.generate_done_summary(conn, task_id, force=True)
    assert dts.get_record(conn, task_id)["summary_source"] == dts.SUMMARY_SOURCE_LLM


# ---------------------------------------------------------------------------
# Read API shape (spec §5.1 / §5.2)
# ---------------------------------------------------------------------------

def test_list_done_returns_the_documented_shape(conn, fake_llm):
    fake_llm(summary="Wrote up the printer problem and what to do about it.")
    task_id = _complete(conn, title="Investigate the printer", metadata={"artifacts": ["/r.md"]})

    result = dts.list_done(conn=conn)
    assert set(result) == {"board", "now", "total", "returned", "items"}
    assert result["returned"] == len(result["items"]) == 1
    assert result["total"] == 1

    item = result["items"][0]
    for key in ("task_id", "title", "assignee", "created_by", "completed_at", "summary",
                "summary_source", "artifacts", "artifact_count", "review_flag",
                "review_reasons", "review_reason", "review_rule_version", "archive_state",
                "archived_at", "archived_by", "generated_at", "fallback"):
        assert key in item, f"the read API must expose {key}"
    assert item["task_id"] == task_id
    assert item["summary"] == "Wrote up the printer problem and what to do about it."
    assert isinstance(item["review_flag"], bool)
    assert isinstance(item["review_reasons"], list)
    assert item["fallback"] is False


def test_list_done_orders_newest_first(conn, fake_llm):
    fake_llm()
    first = _complete(conn, title="Older")
    second = _complete(conn, title="Newer")
    conn.execute("UPDATE tasks SET completed_at = 100 WHERE id = ?", (first,))
    conn.execute("UPDATE tasks SET completed_at = 200 WHERE id = ?", (second,))
    conn.commit()
    items = dts.list_done(conn=conn)["items"]
    assert [i["task_id"] for i in items] == [second, first]


def test_list_done_never_hides_a_task_whose_summary_is_pending(conn, fake_llm):
    """A done task the sweep has not reached must still be listed, readable, not blank."""
    fake_llm()
    task_id = _complete(conn, title="Waiting for the sweep")
    conn.execute("DELETE FROM task_done_summaries WHERE task_id = ?", (task_id,))
    conn.commit()

    item = next(i for i in dts.list_done(conn=conn)["items"] if i["task_id"] == task_id)
    assert item["summary_source"] == dts.SUMMARY_SOURCE_PENDING
    assert item["summary"], "a pending card must still carry readable text"
    assert item["fallback"] is True


def test_list_done_only_review_filters(conn, fake_llm):
    fake_llm()
    clean = _complete(conn, title="Write a haiku", body="about printers",
                      metadata={"artifacts": ["/haiku.md"]})
    flagged = _complete(conn, title="Pay the invoice", body="money owed",
                        metadata={"artifacts": ["/invoice.md"]})

    ids = [i["task_id"] for i in dts.list_done(conn=conn, only_review=True)["items"]]
    assert flagged in ids
    assert clean not in ids


def test_list_done_reports_the_review_reason(conn, fake_llm):
    fake_llm()
    task_id = _complete(conn, title="Pay the invoice", body="money owed")
    dts.generate_pending(conn)
    item = next(i for i in dts.list_done(conn=conn)["items"] if i["task_id"] == task_id)
    assert item["review_flag"] is True
    assert "R2" in item["review_reasons"]
    assert item["review_reason"] == "Touches money or pricing."


def test_list_done_paginates(conn, fake_llm):
    fake_llm()
    for i in range(4):
        _complete(conn, title=f"Task {i}")
    page = dts.list_done(conn=conn, limit=2, offset=1)
    assert page["total"] == 4
    assert page["returned"] == 2


def test_get_done_detail_includes_the_expansion_fields(conn, fake_llm):
    fake_llm()
    task_id = _complete(conn, title="Do the thing", body="the body text")
    detail = dts.get_done_detail(task_id, conn=conn)
    for key in ("body", "result", "runs", "events_tail", "attachments", "parents", "children",
                "prompt_version"):
        assert key in detail, f"detail must expose {key}"
    assert detail["body"] == "the body text"
    assert detail["prompt_version"] == dts.PROMPT_VERSION


def test_get_done_detail_returns_none_for_an_unknown_id(conn, fake_llm):
    fake_llm()
    assert dts.get_done_detail("t_missing", conn=conn) is None


# ---------------------------------------------------------------------------
# Archive: soft, reversible, never a status write (spec §4)
# ---------------------------------------------------------------------------

def test_archive_is_soft_and_reversible(conn, fake_llm):
    fake_llm()
    task_id = _complete(conn)
    dts.generate_pending(conn)

    result = dts.set_archive_state(conn, [task_id], "archived", actor="chad.d@x")
    assert result["ok"] is True and result["changed"] == [task_id]
    assert kb.get_task(conn, task_id).status == "done", "must never touch tasks.status"
    assert task_id not in [i["task_id"] for i in dts.list_done(conn=conn)["items"]]
    assert task_id in [i["task_id"] for i in dts.list_done(conn=conn, include_archived=True)["items"]]
    assert dts.get_done_detail(task_id, conn=conn)["archive_state"] == "archived"

    dts.set_archive_state(conn, [task_id], "active", actor="chad.d@x")
    row = conn.execute(
        "SELECT archive_state, archived_at, archived_by, archive_requested_at, "
        "archive_requested_by FROM task_done_summaries WHERE task_id = ?", (task_id,)).fetchone()
    assert tuple(row) == ("active", None, None, None, None), "all four fields must be cleared"
    assert task_id in [i["task_id"] for i in dts.list_done(conn=conn)["items"]]


def test_archive_requires_an_actor(conn, fake_llm):
    fake_llm()
    task_id = _complete(conn)
    with pytest.raises(ValueError, match="actor is required"):
        dts.set_archive_state(conn, [task_id], "archived", actor="  ")


def test_archive_reports_unknown_and_skipped_without_failing_the_batch(conn, fake_llm):
    fake_llm()
    done = _complete(conn)
    running = kb.create_task(conn, title="Still going", assignee="a", created_by="b")
    result = dts.set_archive_state(conn, [done, "t_ghost", running], "archived", actor="a@b")
    assert result["changed"] == [done]
    assert result["unknown"] == ["t_ghost"]
    assert result["skipped"] == [{"task_id": running, "reason": "task is not done"}]


def test_archive_is_idempotent(conn, fake_llm):
    fake_llm()
    task_id = _complete(conn)
    dts.set_archive_state(conn, [task_id], "archived", actor="a@b")
    again = dts.set_archive_state(conn, [task_id], "archived", actor="a@b")
    assert again["already"] == [task_id]
    assert again["changed"] == []
    assert again["ok"] is True


def test_archive_rejects_an_empty_batch(conn):
    with pytest.raises(ValueError, match="no task_ids"):
        dts.set_archive_state(conn, [], "archived", actor="a@b")


def test_regeneration_preserves_archive_state(conn, fake_llm):
    """A rules-version bump must not lose the owner's archive decision (spec §3)."""
    fake_llm()
    task_id = _complete(conn)
    dts.set_archive_state(conn, [task_id], "archived", actor="a@b")
    dts.generate_done_summary(conn, task_id, force=True)
    assert dts.get_record(conn, task_id)["archive_state"] == "archived"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_schema_is_additive_and_carries_the_plug_in_table(conn):
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    assert "task_done_summaries" in names
    assert "tasks" in names
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "done_summary" not in cols, "the plug-in must not add columns to tasks"


def test_delete_task_relations_cleans_the_plug_in_table(conn, fake_llm):
    fake_llm()
    task_id = _complete(conn)
    dts.generate_done_summary(conn, task_id)
    assert dts.get_record(conn, task_id) is not None
    kb.delete_task(conn, task_id)
    assert dts.get_record(conn, task_id) is None

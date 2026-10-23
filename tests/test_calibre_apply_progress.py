"""Apply reports its current book before slow work and counts only completed outcomes."""
from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations" / "calibre"))
import calibre_fixtures as fixtures
from bl_catalog_calibre.apply import ApplyRefused, apply_matches
from bl_catalog_calibre.config import ApplyOptions
from test_calibre_cover_receipts import make_cover_controller

TERMINAL_PHASES = {"written", "refused", "failed", "cancelled"}


def test_progress_orders_book_phases_and_reports_audited_completion_counts(tmp_path):
    controller, _database, _lookups, _raw = make_cover_controller(tmp_path, count=2)
    events = []
    audited = []
    def progress(done, total, event):
        events.append((done, total, event))
        if event["phase"] in TERMINAL_PHASES:
            audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
            audited.append((done, len(audit), audit[-1]["id"], audit[-1]["status"]))
    results = controller.apply([1, 2], progress=progress)
    assert [(event["phase"], event["id"]) for _, _, event in events] == [
        ("checking", 1), ("checking", 2),
        ("checking", 1), ("cover", 1), ("writing", 1), ("verifying", 1), ("written", 1),
        ("checking", 2), ("cover", 2), ("writing", 2), ("verifying", 2), ("written", 2),
        ("refreshing", None),
    ]
    assert all(total == 2 for _, total, _ in events)
    assert [done for done, _, _ in events] == sorted(done for done, _, _ in events)
    assert [(done, event["current"]) for done, _, event in events if event["phase"] == "written"] == [(1, 1), (2, 2)]
    assert all(done == 0 for done, _, event in events[:2])
    assert all(event["title"] == {1: "First", 2: "Second", None: ""}[event["id"]] for _, _, event in events)
    assert events[-1] == (2, 2, {"phase": "refreshing", "id": None, "title": "", "current": 2})
    assert [result["status"] for result in results] == ["written", "written"]
    assert audited == [(1, 1, 1, "written"), (2, 2, 2, "written")]


@pytest.mark.parametrize("held_phase", ["cover", "writing"])
def test_progress_arrives_before_a_slow_cover_or_writer_can_finish(tmp_path, held_phase):
    controller, _database, lookups, _raw = make_cover_controller(tmp_path)
    entered, release = threading.Event(), threading.Event()
    events = []
    owner, name = (lookups, "verified_cover") if held_phase == "cover" else (controller.writer, "set_fields")
    original = getattr(owner, name)
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5), "Test did not release the held Apply operation"
        return original(*args, **kwargs)
    setattr(owner, name, held)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(controller.apply, [1], progress=lambda *args: events.append(args))
        try:
            assert entered.wait(5)
            assert not result.done()
            assert events[-1] == (0, 1, {"phase": held_phase, "id": 1, "title": "First", "current": 1})
            assert not any(event["phase"] in TERMINAL_PHASES for _, _, event in events)
        finally:
            release.set()
        assert result.result(timeout=5)[0]["status"] == "written"
    assert events[-1][2]["phase"] == "refreshing"


@pytest.mark.parametrize("outcome", ["refused", "failed", "cancelled"])
def test_partial_apply_progress_preserves_status_counts_and_successful_row_refresh(tmp_path, outcome):
    controller, database, _lookups, _raw = make_cover_controller(tmp_path, count=2)
    if outcome == "refused":
        database.data[2]["title"] = "Not in the catalogue"
    elif outcome == "failed":
        original = controller.writer.state
        def state(book_id):
            if book_id == 2:
                raise KeyError("second book disappeared")
            return original(book_id)
        controller.writer.state = state
    events = []
    results = controller.apply([1, 2], progress=lambda *args: events.append(args),
                               should_stop=(lambda: bool(database.cover_writes)) if outcome == "cancelled" else None)
    assert [result["status"] for result in results] == ["written", outcome]
    terminal = [(done, event) for done, _, event in events if event["phase"] in TERMINAL_PHASES]
    assert [(done, event["phase"], event["id"], event["current"]) for done, event in terminal] == [
        (1, "written", 1, 1), (2, outcome, 2, 2),
    ]
    assert events[-1][0:2] == (2, 2)
    assert events[-1][2]["phase"] == "refreshing"
    assert controller.rows[0]["applied_at"] == results[0]["at"]
    assert controller.refresh_error == ""


def test_cancellation_reports_each_remaining_target_with_its_own_position(tmp_path):
    books = [fixtures.make_book(title, ["Author"], bid=bid) for bid, title in enumerate(["First", "Second", "Third"], 1)]
    works = [fixtures.make_work(f"w_{book['id']}", book["title"], ["Author"], isbn=None) for book in books]
    writer = fixtures.FakeWriter(books)
    events = []
    results = apply_matches([1, 2, 3], books=books, lookups=fixtures.FakeLookups(works), writer=writer,
                            options=ApplyOptions(), should_stop=lambda: bool(writer.writes),
                            progress=lambda *args: events.append(args), log_path=tmp_path / "audit.jsonl")
    assert [result["status"] for result in results] == ["written", "cancelled", "cancelled"]
    assert [(done, event["id"], event["current"]) for done, _, event in events if event["phase"] == "cancelled"] == [
        (2, 2, 2), (3, 3, 3),
    ]


def test_progress_observer_failure_cannot_change_writes_audit_or_refresh(tmp_path, caplog):
    controller, database, _lookups, _raw = make_cover_controller(tmp_path, count=2)
    def progress(*_args):
        raise RuntimeError("progress observer closed")
    results = controller.apply([1, 2], progress=progress)
    assert [result["status"] for result in results] == ["written", "written"]
    assert database.cover_writes == [1, 2]
    assert controller.refresh_error == ""
    assert all(row["applied_at"] for row in controller.rows)
    assert [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()] == results
    assert "Could not report Calibre Apply progress" in caplog.text
    assert "Could not report Calibre Apply refresh progress" in caplog.text


def test_duplicate_guard_still_classifies_all_targets_before_any_write_phase(tmp_path):
    books = [fixtures.make_book("First", ["Author"], bid=bid) for bid in (1, 2)]
    work = fixtures.make_work("w_shared", "First", ["Author"], isbn=None)
    writer = fixtures.FakeWriter(books)
    events = []
    results = apply_matches([1, 2], books=books, lookups=fixtures.FakeLookups([work]), writer=writer,
                            progress=lambda *args: events.append(args), log_path=tmp_path / "audit.jsonl")
    assert [result["status"] for result in results] == ["refused", "refused"]
    assert [(done, event["phase"], event["current"]) for done, _, event in events] == [
        (0, "checking", 1), (0, "checking", 2), (1, "refused", 1), (2, "refused", 2),
    ]
    assert writer.writes == []


def test_invalid_batch_emits_no_progress_and_writes_nothing():
    book = fixtures.make_book("First", ["Author"])
    writer = fixtures.FakeWriter([book])
    events = []
    with pytest.raises(ApplyRefused, match="repeats"):
        apply_matches([1, 1], books=[book], lookups=fixtures.FakeLookups(), writer=writer,
                      progress=lambda *args: events.append(args))
    assert events == []
    assert writer.writes == []

"""An unreadable later book must not conceal metadata already written in the batch."""
from __future__ import annotations

import json

import pytest
from test_calibre_cover_receipts import make_cover_controller


@pytest.mark.parametrize("unreadable_id", [1, 2])
def test_live_state_failure_halts_before_that_books_write_and_refreshes_earlier_successes(
    tmp_path, unreadable_id,
):
    controller, database, _lookups, _raw = make_cover_controller(tmp_path, count=2)
    original_state = controller.writer.state

    def read_state(book_id):
        if book_id == unreadable_id:
            raise KeyError(f"Book {book_id} disappeared before its write")
        return original_state(book_id)

    controller.writer.state = read_state
    results = controller.apply([1, 2])

    assert [entry["status"] for entry in results] == ["written"] * (unreadable_id - 1) + ["failed"]
    failed = results[-1]
    assert failed["id"] == unreadable_id
    assert failed["stage"] == "before_write"
    assert f"Book {unreadable_id} disappeared before its write" in failed["error"]
    assert "rolled_back" not in failed
    assert database.cover_writes == list(range(1, unreadable_id))
    assert controller.refresh_error == ""
    cache = controller.scan_cache.load()
    for bid in range(unreadable_id, 3):
        assert database.data[bid]["identifiers"] == {}
        assert bid not in database.covers
        assert "applied_at" not in controller.rows[bid - 1]
        assert cache[bid]["row"]["bl_catalog"] == ""
    if unreadable_id == 2:
        assert database.data[1]["identifiers"] == {"bl_catalog": "w_1"}
        assert controller.rows[0]["bucket"] == "already"
        assert controller.rows[0]["applied_at"] == results[0]["at"]
        assert controller.rows[0]["cover_receipt"]
        assert cache[1]["row"]["bl_catalog"] == "w_1"
        assert cache[1]["row"]["applied_at"] == results[0]["at"]
    logged = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert logged == results


def test_a_failed_read_back_after_the_writes_restores_every_written_field(tmp_path):
    """set_fields reads the book once before writing and once after; when the second read
    raises, the writes already happened and the caller never sees a successful return, so
    set_fields itself must undo them before re-raising."""
    controller, database, _lookups, raw = make_cover_controller(tmp_path)
    writer = controller.writer
    database.covers[1] = b"old cover"
    original_field_for, original_set_field = database.field_for, database.set_field
    reads = {"armed": False, "failed": False}  # fail exactly one read, the first after a write

    def set_field(field, values):
        original_set_field(field, values)
        reads["armed"] = not reads["failed"]

    def field_for(field, bid):
        if reads["armed"]:
            reads["armed"], reads["failed"] = False, True
            raise OSError("metadata read failed right after the write")
        return original_field_for(field, bid)

    database.set_field, database.field_for = set_field, field_for
    state = writer.state(1)["state"]
    changes = {"identifiers": {"bl_catalog": "w_1"}, "comments": "Rewritten"}
    with pytest.raises(OSError, match="right after the write"):
        writer.set_fields(1, changes, expected=state, cover_bytes=raw)
    assert database.data[1]["identifiers"] == {}
    assert database.data[1]["comments"] == state["comments"]
    # the new cover went in, then the old bytes went back through set_cover
    assert database.cover_writes == [1, 1]
    assert database.covers[1] == b"old cover-stored-representation"
    assert writer.state(1)["state"] == state
    assert writer.restore_last(1) is False

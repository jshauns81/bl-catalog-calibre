"""Written metadata, buckets and cached rows must agree before Apply returns."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations" / "calibre"))
from bl_catalog_calibre.apply import CalibreDbWriter
from bl_catalog_calibre.config import DEFAULTS, ApplyOptions
from bl_catalog_calibre.dialog import DialogController
from bl_catalog_calibre.scan_cache import ScanCache
from test_calibre_apply_normalization import Database
from test_calibre_scan_cache import Catalogue, Library


def make_controller(tmp_path):
    books = Library().books
    books.append({**books[0], "id": 2, "title": "Unmatched"})
    database = Database(books, str.strip)
    catalogue = Catalogue()
    writer = CalibreDbWriter(database)
    controller = DialogController(writer, prefs={**DEFAULTS, "log_path": str(tmp_path / "audit.jsonl")},
                                  lookups=catalogue,
                                  scan_cache=ScanCache(tmp_path / "cache.sqlite", "test-library"))
    controller.scan()
    return controller, database, catalogue


def test_apply_refreshes_written_row_and_saved_cache_without_full_scan(tmp_path):
    ctl, _database, catalogue = make_controller(tmp_path)
    unchanged = dict(ctl.rows[1])
    calls = catalogue.calls
    results = ctl.apply([1])
    assert results[0]["status"] == "written"
    assert catalogue.calls - calls == 2  # one apply match and one affected-row refresh
    assert ctl.refresh_error == ""
    assert ctl.rows[0]["bucket"] == "already"
    assert ctl.rows[0]["bl_catalog"] == results[0]["work_id"]
    assert ctl.rows[0]["applied_at"] == results[0]["at"]
    assert ctl.rows[0]["updated_fields"] == ["identifiers"]
    assert ctl.rows[0]["proposals"] == {}
    assert ctl.rows[1] == unchanged
    assert ctl.bucket_counts()["already"] == 1
    assert ctl.plan_apply() == ([], 0)
    catalogue.fail = True
    reloaded = DialogController(ctl.writer, prefs=ctl.prefs, lookups=catalogue, scan_cache=ctl.scan_cache)
    assert reloaded.restore_cache()
    assert reloaded.rows[0]["bucket"] == "already"
    assert reloaded.rows[0]["applied_at"] == results[0]["at"]


def test_clear_filters_and_hide_stamped_do_not_match_or_modify_rows(tmp_path):
    ctl, database, catalogue = make_controller(tmp_path)
    ctl.apply([1])
    catalogue.fail = True
    ctl.hide_stamped = True
    assert [r["id"] for r in ctl.filtered_rows()] == [2]
    ctl.bucket_filter = "already"
    ctl.search_filter = "nothing"
    assert ctl.filtered_rows() == []
    ctl.clear_filters()
    assert [r["id"] for r in ctl.filtered_rows()] == [1, 2]
    assert ctl.bucket_filter == "all" and not ctl.search_filter and not ctl.hide_stamped
    assert database.data[1]["identifiers"]["bl_catalog"]


def test_refresh_failure_does_not_hide_successful_writes(tmp_path, monkeypatch):
    ctl, database, _catalogue = make_controller(tmp_path)
    monkeypatch.setattr(ctl, "_refresh_written_rows", lambda *args: (_ for _ in ()).throw(OSError("read failed")))
    result = ctl.apply([1])[0]
    assert result["status"] == "written"
    assert database.data[1]["identifiers"]["bl_catalog"] == result["work_id"]
    assert ctl.refresh_error == "read failed"


def test_partial_failure_refreshes_successes_without_marking_rolled_back_book(tmp_path):
    ctl, database, catalogue = make_controller(tmp_path)
    for bid in database.data:
        database.data[bid]["identifiers"] = {"bl_catalog": f"w_{bid:024d}"}
        database.data[bid]["comments"] = "Original"
    work = catalogue.works("Test", ["Author"])[0]
    catalogue.work = lambda ident: {**work, "id": ident, "synopsis": {"value": "New synopsis"}}
    ctl.set_options(ApplyOptions(identifiers=False, synopsis=True))
    ctl.scan()
    original_set = database.set_field
    def set_field(field, values):
        if field == "comments" and values.get(2) == "New synopsis":
            values = {2: "Corrupted text"}
        original_set(field, values)
    database.set_field = set_field
    results = ctl.apply([1, 2])
    assert [r["status"] for r in results] == ["written", "failed"]
    assert results[1]["rolled_back"]
    assert ctl.rows[0]["applied_at"]
    assert not ctl.rows[1].get("applied_at")
    assert ctl.books_by_id[1]["comments"] == "New synopsis"
    assert ctl.books_by_id[2]["comments"] == "Original"

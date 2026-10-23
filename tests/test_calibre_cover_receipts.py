"""Successful cover writes stay visible without repeatedly offering the same image."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations" / "calibre"))
import calibre_fixtures as fixtures
from bl_catalog_calibre.apply import CalibreDbWriter
from bl_catalog_calibre.config import DEFAULTS, ApplyOptions
from bl_catalog_calibre.dialog import DialogController
from bl_catalog_calibre.scan_cache import ScanCache
from test_calibre_apply_normalization import Database
from test_calibre_snapshot_runtime import cover_books_and_works


class CoverDatabase(Database):
    def __init__(self, books):
        super().__init__(books, str.strip)
        self.covers = {}
        self.cover_reads = []
        self.cover_writes = []

    def cover(self, bid):
        self.cover_reads.append(bid)
        return self.covers.get(bid)

    def set_cover(self, values):
        self.cover_writes.extend(values)
        self.covers.update({bid: raw + b"-stored-representation" if raw else None for bid, raw in values.items()})


def make_cover_controller(tmp_path, *, count=1, writer_class=CalibreDbWriter):
    raw, books, works = cover_books_and_works()
    database = CoverDatabase(books[:count])
    lookups = fixtures.FakeLookups(works, covers={work["id"]: raw for work in works})
    lookups.cover_calls = []
    original = lookups.verified_cover
    def verified_cover(wid, expected_sha256=None):
        lookups.cover_calls.append(wid)
        return original(wid, expected_sha256)
    lookups.verified_cover = verified_cover
    controller = DialogController(writer_class(database),
                                  prefs={**DEFAULTS, "log_path": str(tmp_path / "audit.jsonl")}, lookups=lookups,
                                  scan_cache=ScanCache(tmp_path / "scans.sqlite", "cover-library"))
    controller.set_options(ApplyOptions(cover=True))
    controller.scan()
    return controller, database, lookups, raw


def test_stored_cover_receipt_survives_toggles_rescan_and_restart_without_cover_reads(tmp_path):
    controller, database, lookups, raw = make_cover_controller(tmp_path)
    result = controller.apply([1])[0]
    receipt = {
        "catalogue_sha256": hashlib.sha256(raw).hexdigest(),
        "stored_sha256": hashlib.sha256(database.covers[1]).hexdigest(),
    }
    assert receipt["catalogue_sha256"] != receipt["stored_sha256"]
    assert result["status"] == "written"
    assert controller.rows[0]["cover_receipt"] == receipt
    assert "cover" not in controller.rows[0]["proposals"]
    assert controller.plan_apply() == ([], 0)
    reads = list(database.cover_reads)
    controller.set_options(ApplyOptions(cover=False))
    controller.set_options(ApplyOptions(cover=True))
    controller.scan(force=True)
    assert controller.rows[0]["cover_receipt"] == receipt
    assert "cover" not in controller.rows[0]["proposals"]
    assert database.cover_reads == reads
    restored = DialogController(controller.writer, prefs=controller.prefs, lookups=lookups, scan_cache=controller.scan_cache)
    assert restored.restore_cache()
    assert restored.rows[0]["cover_receipt"] == receipt
    assert "cover" not in restored.rows[0]["proposals"]
    assert database.cover_reads == reads


def test_explicit_repeat_apply_verifies_selected_cover_and_refuses_no_op(tmp_path):
    controller, database, lookups, _raw = make_cover_controller(tmp_path)
    controller.apply([1])
    reads = len(database.cover_reads)
    result = controller.apply([1])[0]
    assert result["status"] == "refused"
    assert result["error"] == "no new identifiers or selected metadata fields to write"
    assert database.cover_reads[reads:] == [1]
    assert database.cover_writes == [1]
    assert lookups.cover_calls == ["w_1"]


def test_in_memory_rescan_preserves_receipt_when_persistent_cache_is_disabled(tmp_path):
    controller, database, _lookups, _raw = make_cover_controller(tmp_path)
    controller.apply([1])
    receipt = dict(controller.rows[0]["cover_receipt"])
    controller.scan_cache = None
    reads = list(database.cover_reads)
    controller.scan(force=True)
    assert controller.rows[0]["cover_receipt"] == receipt
    assert "cover" not in controller.rows[0]["proposals"]
    assert database.cover_reads == reads


@pytest.mark.parametrize("changed_cover", [b"a cover edited outside the plugin", None])
def test_lazy_validation_invalidates_only_selected_cover_and_persists_proposal(tmp_path, changed_cover):
    controller, database, _lookups, _raw = make_cover_controller(tmp_path, count=2)
    controller.apply([1, 2])
    assert controller.validate_cover_receipt(1)
    database.covers[1] = changed_cover
    reads = len(database.cover_reads)
    assert not controller.validate_cover_receipt(1)
    assert database.cover_reads[reads:] == [1]
    assert "cover" in controller.rows[0]["proposals"]
    assert "cover_receipt" not in controller.rows[0]
    assert "cover_receipt" not in controller.scan_cache.load()[1]["row"]
    assert controller.rows[1]["cover_receipt"]
    assert "cover" not in controller.rows[1]["proposals"]


def test_apply_rechecks_changed_cover_even_when_saved_preview_has_no_cover_proposal(tmp_path):
    controller, database, lookups, _raw = make_cover_controller(tmp_path)
    controller.apply([1])
    database.covers[1] = b"external replacement"
    assert "cover" not in controller.rows[0]["proposals"]
    result = controller.apply([1])[0]
    assert result["status"] == "written"
    assert result["fields_updated"] == ["cover"]
    assert database.cover_writes == [1, 1]
    assert lookups.cover_calls == ["w_1", "w_1"]
    assert controller.validate_cover_receipt(1)
    assert "cover" not in controller.rows[0]["proposals"]


def test_new_catalogue_cover_offers_update_without_reading_current_covers(tmp_path):
    controller, database, lookups, _raw = make_cover_controller(tmp_path)
    controller.apply([1])
    changed = b"new catalogue cover"
    lookups.by_id["w_1"]["cover"]["sha256"] = hashlib.sha256(changed).hexdigest()
    lookups.covers["w_1"] = changed
    lookups.version = "new catalogue"
    reads = list(database.cover_reads)
    controller.scan()
    assert database.cover_reads == reads
    assert "cover" in controller.rows[0]["proposals"]
    assert controller.apply([1])[0]["status"] == "written"
    assert controller.rows[0]["cover_receipt"]["catalogue_sha256"] == hashlib.sha256(changed).hexdigest()
    assert "cover" not in controller.rows[0]["proposals"]


def test_changed_book_metadata_does_not_reuse_cover_receipt(tmp_path):
    controller, database, _lookups, _raw = make_cover_controller(tmp_path)
    controller.apply([1])
    database.data[1]["tags"] = ["external metadata edit"]
    reads = list(database.cover_reads)
    controller.scan()
    assert "cover_receipt" not in controller.rows[0]
    assert "cover" in controller.rows[0]["proposals"]
    assert database.cover_reads == reads


def test_post_write_cover_hash_failure_does_not_hide_success_or_prevent_cache_refresh(tmp_path):
    class UnreadableNewCover(CalibreDbWriter):
        def cover_bytes(self, bid):
            if self.db.covers.get(bid):
                raise OSError("new cover cannot be read")
            return super().cover_bytes(bid)
    controller, database, _lookups, _raw = make_cover_controller(tmp_path, writer_class=UnreadableNewCover)
    result = controller.apply([1])[0]
    assert result["status"] == "written"
    assert database.cover_writes == [1]
    assert controller.refresh_error == ""
    assert controller.rows[0]["applied_at"] == result["at"]
    assert "cover_receipt" not in controller.rows[0]
    assert controller.rows[0]["cover_receipt_error"] == "new cover cannot be read"
    assert controller.scan_cache.load()[1]["row"]["applied_at"] == result["at"]


def test_refresh_restores_version_after_http_cover_budget_reset(tmp_path):
    controller, _database, lookups, _raw = make_cover_controller(tmp_path)
    original_work = lookups.work
    original_works = lookups.works
    def begin():
        lookups.version = None
    def work(wid):
        lookups.version = "fresh-version"
        return original_work(wid)
    def works(*args):
        lookups.version = "fresh-version"
        return original_works(*args)
    lookups.begin, lookups.work, lookups.works = begin, work, works
    result = controller.apply([1])[0]
    assert result["status"] == "written"
    assert controller.catalog_version == "fresh-version"
    assert controller.rows[0]["matched_catalog_version"] == "fresh-version"
    assert controller.scan_cache.load()[1]["version"] == "fresh-version"


def test_cover_backup_failure_returns_earlier_successes_and_refreshes_them(tmp_path):
    class UnreadableSecondCover(CalibreDbWriter):
        def cover_bytes(self, bid):
            if bid == 2:
                raise OSError("second cover backup cannot be read")
            return super().cover_bytes(bid)
    controller, database, _lookups, _raw = make_cover_controller(tmp_path, count=2, writer_class=UnreadableSecondCover)
    results = controller.apply([1, 2])
    assert [row["status"] for row in results] == ["written", "failed"]
    assert results[1]["error"] == "second cover backup cannot be read"
    assert database.cover_writes == [1]
    assert controller.rows[0]["cover_receipt"]
    assert controller.rows[0]["applied_at"] == results[0]["at"]
    assert controller.refresh_error == ""

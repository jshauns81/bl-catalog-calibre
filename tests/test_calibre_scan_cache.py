"""Persistent delta scans across controller restarts and changing library/catalogue state."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations" / "calibre"))
from bl_catalog_calibre.config import DEFAULTS, ApplyOptions
from bl_catalog_calibre.dialog import ApplyRefused, DialogController
from bl_catalog_calibre.scan_cache import ScanCache


class Library:
    def __init__(self):
        self.books = [{"id": 1, "title": "Test", "authors": ["Author"], "identifiers": {},
                       "comments": "", "tags": [], "formats": ["EPUB"]}]

    def read_books(self, ids=None):
        return copy.deepcopy(self.books)


class Catalogue:
    version = "one"

    def __init__(self):
        self.calls = 0
        self.fail = False

    def begin(self):
        self.calls += 1
        if self.fail:
            raise AssertionError("Unchanged book was unnecessarily matched")

    def work(self, ident):
        return self.works("Test", ["Author"])[0]

    def works(self, title, authors):
        return [{"id": "w_" + "a" * 24, "title": {"value": "Test"},
                 "authors": [{"name": "Author", "role": "author", "score": 1}], "editions": []}]


@pytest.fixture
def setup(tmp_path):
    library, catalogue = Library(), Catalogue()
    def controller(name="library-a"):
        return DialogController(library, prefs=dict(DEFAULTS), lookups=catalogue,
                                scan_cache=ScanCache(tmp_path / "scans.sqlite", name))
    return library, catalogue, controller


def test_restart_restores_without_network_and_delta_reuses(setup):
    _library, catalogue, factory = setup
    first = factory()
    assert first.scan()[0]["bucket"] == "certain_title_author"
    assert catalogue.calls == 1
    catalogue.fail = True
    second = factory()
    assert second.restore_cache()
    assert second.cache_needs_check
    with pytest.raises(ApplyRefused, match="Scan Changes"):
        second.apply([1])
    second.scan()
    assert not second.cache_needs_check
    assert second.scan_stats == {"reused": 1, "matched": 0}
    second.set_options(ApplyOptions(identifiers=False, title=True))
    assert second.rows[0]["add"] == {}


def test_added_changed_deleted_and_duplicate_recovery(setup):
    library, catalogue, factory = setup
    factory().scan()
    library.books.append({**library.books[0], "id": 2})
    ctl = factory()
    assert all(r["bucket"] == "ambiguous" for r in ctl.scan())
    assert ctl.scan_stats == {"reused": 1, "matched": 1}
    assert catalogue.calls == 2
    library.books.pop()
    assert factory().scan()[0]["bucket"] == "certain_title_author"
    assert catalogue.calls == 2
    library.books[0]["tags"] = ["changed"]
    factory().scan()
    assert catalogue.calls == 3
    assert set(ctl.scan_cache.load()) == {1}


def test_catalogue_version_force_source_and_library_isolation(setup):
    _library, catalogue, factory = setup
    factory().scan()
    catalogue.version = "two"
    factory().scan()
    assert catalogue.calls == 2
    ctl = factory()
    ctl.scan(force=True)
    assert catalogue.calls == 3
    ctl.prefs["url"] = "https://another.example"
    ctl.scan()
    assert catalogue.calls == 4
    assert not factory("library-b").restore_cache()
    factory("library-b").scan()
    assert catalogue.calls == 5


def test_cancel_does_not_replace_saved_scan(setup):
    library, _catalogue, factory = setup
    ctl = factory()
    ctl.scan()
    before = ctl.scan_cache.load()
    library.books[0]["title"] = "Edited"
    def cancel(*args):
        raise RuntimeError("cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        ctl.scan(progress=cancel)
    assert ctl.scan_cache.load() == before
    assert ctl.rows[0]["title"] == "Test"


def test_selected_scan_preserves_other_cached_books(setup):
    library, _catalogue, factory = setup
    library.books.append({**library.books[0], "id": 2})
    factory().scan()
    ctl = factory()
    ctl.scan([1], force=True)
    assert len(ctl.rows) == 1
    assert set(ctl.scan_cache.load()) == {1, 2}
    assert len(factory().scan()) == 2


def test_corrupt_or_unwritable_cache_does_not_prevent_scan(setup, tmp_path):
    _library, _catalogue, factory = setup
    ctl = factory()
    ctl.scan_cache.path.write_bytes(b"broken sqlite")
    assert ctl.scan()[0]["bucket"] == "certain_title_author"
    assert ctl.scan_cache.error
    ctl.scan_cache.path = tmp_path  # directory, cannot open as SQLite
    ctl.scan()
    assert ctl.scan_cache.error


def test_unknown_version_never_reuses_cache(setup):
    _library, catalogue, factory = setup
    factory().scan()
    catalogue.version = None
    factory().scan()
    factory().scan()
    assert catalogue.calls == 3


def test_manual_pick_survives_restart(setup):
    _library, catalogue, factory = setup
    ctl = factory()
    ctl.scan()
    work = catalogue.works("Test", ["Author"])[0]
    ctl.pick_work_for_book(1, work)
    restarted = factory()
    restarted.restore_cache()
    assert restarted.rows[0]["picked"]
    restarted.scan()
    assert restarted.rows[0]["picked"]


def test_catalogue_refresh_revalidates_manual_picks(setup):
    _library, catalogue, factory = setup
    ctl = factory()
    ctl.scan()
    ctl.pick_work_for_book(1, catalogue.works("Test", ["Author"])[0])
    catalogue.version = "new"
    assert factory().scan()[0]["picked"]


def test_snapshot_checks_server_version_and_refreshes_before_delta(setup, monkeypatch):
    from types import SimpleNamespace

    from bl_catalog_calibre import dialog

    _library, catalogue, factory = setup
    class Snapshot:
        def __init__(self, version):
            self.version = version
            self.http_client = SimpleNamespace(fetch_version=lambda: "two")
            self.closed = False
        def close(self):
            self.closed = True
    old, new = Snapshot("one"), Snapshot("two")
    monkeypatch.setattr(dialog, "SnapshotCatalogClient", Snapshot)
    ctl = factory()
    ctl.scan()
    ctl._injected_lookups = None
    catalogue.backend = old
    def sync():
        catalogue.backend = new
        catalogue.version = "two"
    ctl.sync_snapshot = sync
    ctl.build_lookups = lambda: catalogue
    ctl.scan()
    assert old.closed and new.closed
    assert ctl.scan_stats == {"reused": 0, "matched": 1}
    assert ctl.catalog_version == "two"


def test_transient_lookup_errors_are_retried(setup, monkeypatch):
    from bl_catalog_calibre.matcher import CatalogueError

    _library, catalogue, factory = setup
    original = catalogue.works
    def fail(*args):
        raise CatalogueError("temporary outage")
    monkeypatch.setattr(catalogue, "works", fail)
    ctl = factory()
    assert ctl.scan()[0]["bucket"] == "error"
    assert ctl.scan_cache.load() == {}
    monkeypatch.setattr(catalogue, "works", original)
    assert factory().scan()[0]["bucket"] == "certain_title_author"
    assert catalogue.calls == 2


def test_catalogue_change_during_scan_does_not_commit(setup, monkeypatch):
    from bl_catalog_calibre.matcher import CatalogueError

    _library, catalogue, factory = setup
    original = catalogue.works
    def changed(*args):
        catalogue.version = "changed mid-scan"
        return original(*args)
    monkeypatch.setattr(catalogue, "works", changed)
    ctl = factory()
    with pytest.raises(CatalogueError, match="changed during"):
        ctl.scan()
    assert ctl.scan_cache.load() == {}

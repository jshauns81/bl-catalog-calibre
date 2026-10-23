"""Snapshots stay pinned, and saved matches need only targeted write-time checks."""
from __future__ import annotations

import json
import sqlite3

import pytest
from bl_catalog_calibre import config, snapshot
from bl_catalog_calibre.client import SnapshotCatalogClient
from bl_catalog_calibre.dialog import DialogController
from bl_catalog_calibre.matcher import CatalogueError
from bl_catalog_calibre.scan_cache import ScanCache
from test_calibre_apply_refresh import make_controller
from test_calibre_scan_cache import Catalogue, Library


@pytest.fixture
def setup(tmp_path):
    library, catalogue = Library(), Catalogue()
    def factory():
        return DialogController(library, prefs=dict(config.DEFAULTS), lookups=catalogue,
                                scan_cache=ScanCache(tmp_path / "cache.sqlite", "library"))
    return library, catalogue, factory


def test_snapshot_defaults_to_persistent_configuration():
    assert config.DEFAULT_SNAPSHOT_PATH.endswith("/plugins/bl_catalog/catalogue.sqlite")
    assert "/tmp/" not in config.DEFAULT_SNAPSHOT_PATH


def test_legacy_snapshot_is_copied_once_and_never_overwrites_pinned_data(tmp_path, monkeypatch):
    target = tmp_path / "persistent/catalogue.sqlite"
    legacy = tmp_path / "calibre-old/bl-catalog-latest.sqlite"
    legacy.parent.mkdir()
    legacy.write_bytes(b"old snapshot")
    monkeypatch.setattr(snapshot, "DEFAULT_SNAPSHOT_PATH", str(target))
    monkeypatch.setattr(snapshot.tempfile, "gettempdir", lambda: str(tmp_path))
    assert snapshot.migrate_snapshot_path(legacy) == str(target)
    assert target.read_bytes() == legacy.read_bytes() == b"old snapshot"
    target.write_bytes(b"pinned snapshot")
    assert snapshot.migrate_snapshot_path(legacy) == str(target)
    assert target.read_bytes() == b"pinned snapshot"
    legacy.unlink()
    assert snapshot.migrate_snapshot_path(legacy) == str(target)
    custom = tmp_path / "custom.sqlite"
    assert snapshot.migrate_snapshot_path(custom) == str(custom)


def test_legacy_cache_scope_survives_snapshot_path_migration(setup):
    _library, catalogue, factory = setup
    ctl = factory()
    ctl._cache_scope = lambda: json.dumps([1, ctl.prefs["url"], "http", "/tmp/legacy.sqlite"])
    ctl.scan()
    catalogue.fail = True
    restored = factory()
    assert restored.restore_cache()
    assert not restored.cache_needs_check


def test_missing_snapshot_never_silently_switches_to_live_http(setup, tmp_path):
    library, _catalogue, _factory = setup
    ctl = DialogController(library, prefs={**config.DEFAULTS, "snapshot_path": str(tmp_path / "missing.sqlite")})
    with pytest.raises(CatalogueError, match="No local snapshot"):
        ctl.build_lookups()


def test_apply_after_catalogue_change_rechecks_only_requested_book(tmp_path):
    ctl, _database, catalogue = make_controller(tmp_path)
    original = dict(ctl.rows[1])
    catalogue.version = "two"
    restored = DialogController(ctl.writer, prefs=ctl.prefs, lookups=catalogue, scan_cache=ctl.scan_cache)
    assert restored.restore_cache()
    assert not restored.cache_needs_check
    calls = catalogue.calls
    assert restored.apply([1])[0]["status"] == "written"
    assert catalogue.calls - calls == 2
    assert restored.rows[1] == {**original, "matched_catalog_version": "one"}
    assert restored.rows[0]["matched_catalog_version"] == "two"
    cached = ctl.scan_cache.load()
    assert cached[1]["version"] == "two"
    assert cached[2]["version"] == "one"


def test_new_catalogue_ambiguity_refuses_old_certain_match(tmp_path):
    ctl, database, catalogue = make_controller(tmp_path)
    work = catalogue.works("Test", ["Author"])[0]
    catalogue.version = "two"
    catalogue.works = lambda *_: [work, {**work, "id": "w_" + "b" * 24}]
    result = ctl.apply([1])[0]
    assert result["status"] == "refused"
    assert database.data[1]["identifiers"] == {}


def test_title_lookup_indexes_full_normalized_titles_before_loading_work(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE meta(key TEXT, value TEXT); INSERT INTO meta VALUES('catalog_version','one');"
                         "CREATE TABLE work(id TEXT, title TEXT); CREATE TABLE work_alias(work_id TEXT, title TEXT);")
        db.executemany("INSERT INTO work VALUES(?,?)", [(f"w_{n:04}", f"The Other Book {n}") for n in range(300)])
        db.execute("INSERT INTO work VALUES('w_target','The Target Book')")
        db.execute("INSERT INTO work_alias VALUES('w_target','Target Book, The')")
    client = SnapshotCatalogClient(path)
    assert client.version == "one"
    loaded = []
    def view(wid):
        loaded.append(wid)
        return {"id": wid, "title": {"value": "The Target Book"}, "aliases": ["Target Book, The"]}
    monkeypatch.setattr(client, "_work_view", view)
    try:
        assert client.works("The Target Book")[1][0]["id"] == "w_target"
        assert loaded == ["w_target"]
        assert client.works("Target Book, The")[1][0]["id"] == "w_target"
        assert client.works("The Missing Book")[1] == []
        assert loaded == ["w_target", "w_target"]
    finally:
        client.close()

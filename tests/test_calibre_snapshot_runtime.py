"""Snapshot activation and cover failures must preserve previously successful work."""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations" / "calibre"))
import calibre_fixtures as fixtures
from bl_catalog_calibre import client
from bl_catalog_calibre.apply import CalibreDbWriter, apply_matches
from bl_catalog_calibre.config import DEFAULTS, ApplyOptions
from bl_catalog_calibre.dialog import DialogController
from bl_catalog_calibre.matcher import CatalogueError
from bl_catalog_calibre.scan_cache import ScanCache
from test_calibre_apply_normalization import Database


class Response:
    def __init__(self, raw, headers):
        self.raw = io.BytesIO(raw)
        self.headers = headers

    def read(self, size=-1):
        return self.raw.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class Opener:
    def __init__(self, raw, headers):
        self.raw, self.headers = raw, headers

    def open(self, _request, **_kwargs):
        return Response(self.raw, self.headers)


@pytest.fixture
def snapshot_path(tmp_path):
    path = tmp_path / "download-source.sqlite"
    fixtures.build_snapshot_db(path)
    return path


@pytest.mark.parametrize("problem", ["truncated", "wrong_schema", "missing_version", "wrong_version", "missing_column"])
def test_invalid_download_preserves_pinned_snapshot(snapshot_path, tmp_path, problem):
    target = tmp_path / "catalogue.sqlite"
    shutil.copyfile(snapshot_path, target)
    before = target.read_bytes()
    header_version = "2026.10.21.5"
    if problem == "wrong_schema":
        snapshot_path.unlink()
        with sqlite3.connect(snapshot_path) as db:
            db.execute("CREATE TABLE meta(key TEXT, value TEXT)")
            db.execute("INSERT INTO meta VALUES('catalog_version', ?)", (header_version,))
    elif problem == "missing_version":
        with sqlite3.connect(snapshot_path) as db:
            db.execute("DELETE FROM meta")
    elif problem == "missing_column":
        with sqlite3.connect(snapshot_path) as db:
            db.execute("ALTER TABLE work RENAME COLUMN title TO unsupported_title")
    elif problem == "wrong_version":
        header_version = "a-different-version"
    payload = snapshot_path.read_bytes()
    if problem == "truncated":
        payload = payload[:1024]
    http = client.HttpCatalogClient("http://example.test", opener=Opener(payload, {"X-Catalog-Version": header_version}))
    with pytest.raises(CatalogueError, match="snapshot"):
        http.download_snapshot(target)
    assert target.read_bytes() == before
    assert http.version is None
    assert list(tmp_path.glob(".blc-snap-*.part")) == []


@pytest.mark.parametrize("meta_key", ["catalog_version", "version"])
@pytest.mark.parametrize("header", [True, False])
def test_valid_download_activates_verified_database_version(snapshot_path, tmp_path, meta_key, header):
    with sqlite3.connect(snapshot_path) as db:
        db.execute("UPDATE meta SET key=? WHERE key='version'", (meta_key,))
    target = tmp_path / "catalogue.sqlite"
    target.write_bytes(b"old catalogue")
    payload = snapshot_path.read_bytes()
    headers = {"X-Catalog-Version": "2026.10.21.5"} if header else {}
    http = client.HttpCatalogClient("http://example.test", opener=Opener(payload, headers))
    assert http.download_snapshot(target) == (target, "2026.10.21.5")
    assert target.read_bytes() == payload
    assert http.version == "2026.10.21.5"


def test_snapshot_reader_stays_pinned_across_atomic_replacement(snapshot_path, tmp_path):
    target = tmp_path / "catalogue.sqlite"
    shutil.copyfile(snapshot_path, target)
    reader = client.SnapshotCatalogClient(target)
    try:
        assert reader.conn.in_transaction
        assert reader.work("w_horus")["work"]["title"]["value"] == "Horus Rising"
        with sqlite3.connect(snapshot_path) as db:
            db.execute("UPDATE meta SET value='next-version' WHERE key='version'")
            db.execute("UPDATE work SET title='A New Title' WHERE id='w_horus'")
        http = client.HttpCatalogClient("http://example.test", opener=Opener(
            snapshot_path.read_bytes(), {"X-Catalog-Version": "next-version"},
        ))
        http.download_snapshot(target)
        assert reader.version == "2026.10.21.5"
        assert reader.work("w_horus")["work"]["title"]["value"] == "Horus Rising"
        replacement = client.SnapshotCatalogClient(target)
        try:
            assert replacement.version == "next-version"
            assert replacement.work("w_horus")["work"]["title"]["value"] == "A New Title"
        finally:
            replacement.close()
    finally:
        reader.close()


def cover_books_and_works():
    raw = b"verified cover bytes"
    sha = hashlib.sha256(raw).hexdigest()
    books, works = [], []
    for bid, title in [(1, "First"), (2, "Second")]:
        wid = f"w_{bid}"
        books.append(fixtures.make_book(title, ["Author"], bid=bid))
        works.append(fixtures.make_work(wid, title, ["Author"], isbn=None, cover={
            "url": f"/v1/work/{wid}/cover", "sha256": sha, "width": 1400, "height": 2000,
        }))
    return raw, books, works


def test_snapshot_cover_budget_is_renewed_for_each_written_book(snapshot_path, monkeypatch):
    raw, books, works = cover_books_and_works()
    clock = [0.0]
    monkeypatch.setattr(client.time, "monotonic", lambda: clock[0])
    http = client.HttpCatalogClient("http://example.test", opener=Opener(raw, {"Content-Type": "image/jpeg"}))
    snapshot = client.SnapshotCatalogClient(snapshot_path, http_client=http)
    lookups = fixtures.FakeLookups(works)
    lookups.begin = snapshot.begin
    deadlines = []
    def fetch(wid, expected_sha256=None):
        deadlines.append(http.deadline)
        result = snapshot.verified_cover(wid, expected_sha256)
        clock[0] += 35.0
        return result
    lookups.verified_cover = fetch
    writer = fixtures.FakeWriter(books)
    try:
        results = apply_matches([1, 2], books=books, lookups=lookups, writer=writer,
                                options=ApplyOptions(cover=True))
        assert [row["status"] for row in results] == ["written", "written"]
        assert deadlines == [30.0, 65.0]
        assert set(writer.covers_written) == {1, 2}
        assert snapshot.version == "2026.10.21.5"
    finally:
        snapshot.close()


def test_cover_failure_returns_partial_results_and_refreshes_successful_rows(tmp_path):
    raw, books, works = cover_books_and_works()
    class CoverDatabase(Database):
        def __init__(self):
            super().__init__(books, str.strip)
            self.covers = {}
        def cover(self, bid):
            return self.covers.get(bid)
        def set_cover(self, values):
            self.covers.update(values)
    database = CoverDatabase()
    lookups = fixtures.FakeLookups(works)
    def fetch(wid, expected_sha256=None):
        if wid == "w_2":
            raise CatalogueError("simulated cover outage")
        return raw, "image/jpeg"
    lookups.verified_cover = fetch
    log_path = tmp_path / "audit.jsonl"
    controller = DialogController(CalibreDbWriter(database),
                                  prefs={**DEFAULTS, "log_path": str(log_path)}, lookups=lookups,
                                  scan_cache=ScanCache(tmp_path / "scans.sqlite", "test-library"))
    controller.set_options(ApplyOptions(cover=True))
    controller.scan()
    results = controller.apply([1, 2])
    assert [row["status"] for row in results] == ["written", "failed"]
    assert results[1]["error"] == "cover: simulated cover outage"
    assert controller.refresh_error == ""
    assert controller.rows[0]["applied_at"] == results[0]["at"]
    assert controller.rows[0]["bucket"] == "already"
    assert not controller.rows[1].get("applied_at")
    assert database.data[1]["identifiers"] == {"bl_catalog": "w_1"}
    assert database.data[2]["identifiers"] == {}
    assert database.covers == {1: raw}
    assert controller.scan_cache.load()[1]["row"]["applied_at"] == results[0]["at"]
    assert [json.loads(line)["status"] for line in log_path.read_text().splitlines()] == ["written", "failed"]

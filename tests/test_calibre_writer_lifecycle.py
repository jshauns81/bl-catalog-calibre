"""Library changes must wait for local verification while blocking later writes."""
from __future__ import annotations

import copy
import hashlib
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations" / "calibre"))
import calibre_fixtures as fixtures
from bl_catalog_calibre.apply import ApplyRefused, CalibreDbWriter, StaleBookError, apply_matches
from bl_catalog_calibre.config import ApplyOptions


class Database:
    def __init__(self, book, *, corrupt=False):
        self.book = copy.deepcopy(book)
        self.corrupt = corrupt
        self.writes = []

    def field_for(self, field, bid):
        assert bid == self.book["id"]
        return self.book.get(field)

    def formats(self, bid, verify_formats=False):
        return ("EPUB",)

    def set_field(self, field, values):
        value = values[self.book["id"]]
        self.writes.append((field, value))
        if self.corrupt and field == "identifiers" and value:
            value = {"bl_catalog": "w_wrong"}
        self.book[field] = value

    def cover(self, bid):
        return None


class ObservedLock:
    def __init__(self):
        self.lock = threading.RLock()
        self.detach_waiting = threading.Event()

    def __enter__(self):
        if threading.current_thread().name == "test-library-detach":
            self.detach_waiting.set()
        return self.lock.__enter__()

    def __exit__(self, *args):
        return self.lock.__exit__(*args)


@pytest.mark.parametrize("corrupt", [False, True])
def test_detach_waits_through_readback_and_rollback(corrupt):
    book = fixtures.make_book()
    database = Database(book, corrupt=corrupt)
    writer = CalibreDbWriter(database)
    lock = writer._lifecycle_lock = ObservedLock()
    lookups = fixtures.FakeLookups([fixtures.make_work("w_horus", book["title"], book["authors"])])
    verifying = threading.Event()
    release_verification = threading.Event()
    detached = threading.Event()
    outcomes = []
    errors = []

    def progress(_done, _total, event):
        if event["phase"] == "verifying":
            verifying.set()
            if not release_verification.wait(5):
                raise RuntimeError("Test did not release verification")

    def apply():
        try:
            outcomes.extend(apply_matches([book["id"]], books=[book], lookups=lookups,
                                          writer=writer, progress=progress))
        except Exception as error:  # noqa: BLE001 - report worker failures to the test thread
            errors.append(error)

    def detach():
        writer.detach()
        detached.set()

    worker = threading.Thread(target=apply, name="test-apply")
    detacher = threading.Thread(target=detach, name="test-library-detach")
    worker.start()
    try:
        assert verifying.wait(5), errors
        detacher.start()
        assert lock.detach_waiting.wait(5)
        assert not detached.is_set(), "The database must stay attached until verification finishes"
        assert not writer.available(), "Detachment must prevent another write immediately"
    finally:
        release_verification.set()
        worker.join(5)
        if detacher.ident is not None:
            detacher.join(5)
    assert not worker.is_alive()
    assert not detacher.is_alive()
    assert not errors
    assert detached.is_set()
    result = outcomes[0]
    if corrupt:
        assert result["status"] == "failed"
        assert result["rolled_back"] is True
        assert database.book["identifiers"] == {}
    else:
        assert result["status"] == "written"
        assert database.book["identifiers"]["bl_catalog"] == "w_horus"
    previous_writes = len(database.writes)
    with pytest.raises(ApplyRefused, match="library changed or is closing"):
        writer.set_fields(book["id"], {"title": "Another title"})
    assert len(database.writes) == previous_writes


@pytest.mark.parametrize("detach", [False, True])
def test_cancel_during_cover_download_never_starts_a_write(detach):
    book = fixtures.make_book()
    database = Database(book)
    writer = CalibreDbWriter(database)
    raw = b"verified image bytes"
    work = fixtures.make_work("w_horus", book["title"], book["authors"], cover={
        "url": "/v1/work/w_horus/cover", "sha256": hashlib.sha256(raw).hexdigest(),
        "width": 1400, "height": 2000,
    })
    lookups = fixtures.FakeLookups([work])
    stop = threading.Event()
    downloading = threading.Event()
    resume_download = threading.Event()
    detached = threading.Event()
    outcomes = []
    errors = []

    def download(_wid, expected_sha256=None):
        downloading.set()
        if not resume_download.wait(5):
            raise RuntimeError("Test did not release cover download")
        return raw, "image/jpeg"

    def apply():
        try:
            outcomes.extend(apply_matches([book["id"]], books=[book], lookups=lookups, writer=writer,
                                          options=ApplyOptions(cover=True), should_stop=stop.is_set))
        except Exception as error:  # noqa: BLE001 - report worker failures to the test thread
            errors.append(error)

    def detach_writer():
        writer.detach()
        detached.set()

    lookups.verified_cover = download
    worker = threading.Thread(target=apply, name="test-cover-download")
    detacher = threading.Thread(target=detach_writer, name="test-library-detach")
    worker.start()
    try:
        assert downloading.wait(5), errors
        if detach:
            detacher.start()
            assert detached.wait(5), "Library detachment must not wait for a network download"
        else:
            stop.set()
    finally:
        resume_download.set()
        worker.join(5)
        if detacher.ident is not None:
            detacher.join(5)
    assert not worker.is_alive()
    assert not detacher.is_alive()
    assert not errors
    assert outcomes[0]["status"] == "cancelled"
    assert database.writes == []
    assert database.book == book


def test_stale_write_releases_lifecycle_gate():
    book = fixtures.make_book()
    database = Database(book)
    writer = CalibreDbWriter(database)
    original = writer.state(book["id"])["state"]
    database.book["title"] = "Edited in Calibre"
    with pytest.raises(StaleBookError):
        writer.set_fields(book["id"], {"tags": ["new"]}, expected=original)
    detached = threading.Event()
    detacher = threading.Thread(target=lambda: (writer.detach(), detached.set()))
    detacher.start()
    detacher.join(5)
    assert detached.is_set()
    assert database.writes == []


def test_partial_write_failure_rolls_back_and_releases_lifecycle_gate():
    book = fixtures.make_book()
    database = Database(book)
    set_field = database.set_field

    def fail_tags(field, values):
        if field == "tags":
            raise OSError("Disk write failed")
        set_field(field, values)

    database.set_field = fail_tags
    writer = CalibreDbWriter(database)
    original = writer.state(book["id"])["state"]
    with pytest.raises(OSError, match="Disk write failed"):
        writer.set_fields(book["id"], {"title": "Temporary title", "tags": ["new"]}, expected=original)
    assert writer.state(book["id"])["state"] == original
    detached = threading.Event()
    detacher = threading.Thread(target=lambda: (writer.detach(), detached.set()))
    detacher.start()
    detacher.join(5)
    assert detached.is_set()

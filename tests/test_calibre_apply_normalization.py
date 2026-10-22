"""Calibre comments adaptation must not cause false rollback or hide real data loss."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations" / "calibre"))
from bl_catalog_calibre.apply import CalibreDbWriter, apply_matches
from bl_catalog_calibre.config import ApplyOptions
from bl_catalog_calibre.matcher import build_proposals
from test_calibre_scan_cache import Catalogue


class Database:
    def __init__(self, books, adapter, corrupt=False):
        self.data = {b["id"]: dict(b) for b in books}
        self.adapter = adapter
        self.corrupt = corrupt

    def all_book_ids(self):
        return self.data.keys()

    def field_for(self, field, bid):
        return self.data[bid].get(field)

    def formats(self, bid):
        return ("EPUB",)

    def set_field(self, field, values):
        for bid, val in values.items():
            if field == "comments":
                val = self.adapter(val)
                if self.corrupt and val != "Original":
                    val = "Unrelated text"
            self.data[bid][field] = val


@pytest.fixture(params=[str.strip, lambda value: " ".join(value.split())])
def adapter(request, monkeypatch):
    convert = request.param
    module = ModuleType("calibre.db.write")
    def get_adapter(field, metadata):
        assert field == "comments"
        assert metadata == {"datatype": "text", "is_multiple": False}
        return convert
    module.get_adapter = get_adapter
    monkeypatch.setitem(sys.modules, "calibre.db.write", module)
    return convert


def fixture_book(bid):
    return {"id": bid, "title": "Test", "authors": ["Author"], "comments": "Original",
            "identifiers": {"bl_catalog": "w_" + "a" * 24}, "tags": [], "series": ""}


def catalogue_with_synopsis():
    catalogue = Catalogue()
    work = catalogue.works("Test", ["Author"])[0]
    work["synopsis"] = {"value": "Omnibus edition\n\nFor millennia, Vect has ruled."}
    catalogue.work = lambda ident: work
    return catalogue, work


def test_fourteen_synopsis_writes_use_calibres_conversion_and_do_not_repeat(adapter, tmp_path):
    database = Database([fixture_book(bid) for bid in range(1, 15)], adapter)
    writer = CalibreDbWriter(database)
    catalogue, work = catalogue_with_synopsis()
    # Distinct work ids let all fourteen books pass the existing duplicate-work guard.
    for bid in database.data:
        database.data[bid]["identifiers"] = {"bl_catalog": f"w_{bid:024d}"}
    def lookup(ident):
        return {**work, "id": ident}
    catalogue.work = lookup
    options = ApplyOptions(identifiers=False, synopsis=True)
    results = apply_matches(list(database.data), books=writer.read_books(), lookups=catalogue,
                            writer=writer, log_path=tmp_path / "audit.jsonl", options=options)
    assert [r["status"] for r in results] == ["written"] * 14
    for book in writer.read_books():
        assert book["comments"] == adapter(work["synopsis"]["value"])
        assert "synopsis" not in build_proposals(book, work, options)


def test_content_loss_still_rolls_back_and_records_expected_actual(adapter, tmp_path):
    database = Database([fixture_book(1)], adapter, corrupt=True)
    writer = CalibreDbWriter(database)
    catalogue, work = catalogue_with_synopsis()
    log = tmp_path / "audit.jsonl"
    result = apply_matches([1], books=writer.read_books(), lookups=catalogue, writer=writer,
                           log_path=log, options=ApplyOptions(identifiers=False, synopsis=True))[0]
    assert result["status"] == "failed"
    assert result["rolled_back"]
    assert database.data[1]["comments"] == "Original"
    assert result["verification"] == {
        "field": "comments", "expected": adapter(work["synopsis"]["value"]),
        "actual": "Unrelated text",
    }
    assert json.loads(log.read_text())["verification"] == result["verification"]

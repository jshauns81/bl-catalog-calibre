"""An unknown catalogue position must not offer to clear Calibre's series index."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations" / "calibre"))
import calibre_fixtures as fixtures
from bl_catalog_calibre.apply import CalibreDbWriter
from bl_catalog_calibre.config import DEFAULTS, ApplyOptions
from bl_catalog_calibre.dialog import DialogController
from bl_catalog_calibre.matcher import build_proposals
from test_calibre_apply_normalization import Database


@pytest.mark.parametrize("position,offered", [(None, False), (1, False), (2, True)])
def test_same_series_proposes_only_a_known_changed_position(position, offered):
    book = fixtures.make_book("Virtues of the Sons", ["Andy Smillie"])
    book.update(series="The Horus Heresy", series_index=1.0)
    work = fixtures.make_work("w_virtues", book["title"], book["authors"], isbn=None,
                                  series=[{"name": "The Horus Heresy", "position": position}])
    proposals = build_proposals(book, work, ApplyOptions(identifiers=False, series=True))
    assert ("series" in proposals) is offered


def test_series_write_with_unknown_catalogue_position_has_no_repeat_proposal(tmp_path):
    book = fixtures.make_book("Virtues of the Sons", ["Andy Smillie"], bl_catalog="w_virtues")
    book.update(series="", series_index=1.0)
    work = fixtures.make_work("w_virtues", book["title"], book["authors"], isbn=None,
                                  series=[{"name": "The Horus Heresy", "position": None}])
    database = Database([book], str.strip)
    controller = DialogController(CalibreDbWriter(database),
                                  prefs={**DEFAULTS, "log_path": str(tmp_path / "audit.jsonl")},
                                  lookups=fixtures.FakeLookups([work]))
    controller.set_options(ApplyOptions(identifiers=False, series=True))
    assert controller.scan()[0]["proposals"] == {"series": {"series": "The Horus Heresy", "series_index": None}}
    result = controller.apply([1])[0]
    assert result["status"] == "written"
    assert result["fields_updated"] == ["series"]
    assert database.data[1]["series"] == "The Horus Heresy"
    assert database.data[1]["series_index"] == 1.0
    assert controller.rows[0]["proposals"] == {}
    assert controller.plan_apply() == ([], 0)
    assert controller.apply([1])[0]["status"] == "refused"

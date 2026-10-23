"""Optional native Qt geometry regressions for Calibre's host font settings."""

from __future__ import annotations

import os
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")
from PyQt6 import QtCore, QtGui, QtWidgets

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations" / "calibre"))
from bl_catalog_calibre import dialog as plugin_dialog
from bl_catalog_calibre.config import DEFAULTS
from bl_catalog_calibre.style import render_work_detail_html


@pytest.fixture(scope="module")
def qt_app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _settle(app):
    # Grid reflow posts layout requests, which can post another parent layout request.
    for _ in range(6):
        app.processEvents()


@pytest.fixture
def scanned_dialog(qt_app, request):
    point_size, bold = request.param
    previous_font = QtGui.QFont(qt_app.font())
    font = QtGui.QFont("DejaVu Sans", point_size)
    font.setBold(bold)
    qt_app.setFont(font)
    controller = plugin_dialog.DialogController(object(), prefs=dict(DEFAULTS))
    for bid in range(1, 1882):
        work = {
            "id": f"w_{bid}",
            "title": {"value": f"Canonical Book {bid}"},
            "authors": [{"name": "Fixture Author", "role": "author", "score": 1.0}],
        }
        controller.books_by_id[bid] = {
            "id": bid, "title": f"Book {bid}", "authors": ["Fixture Author"], "identifiers": {},
        }
        controller.rows.append({
            "id": bid, "title": f"Book {bid}", "authors": "Fixture Author",
            "bucket": "certain_title_author", "work_id": work["id"], "work": work,
            "add": {"bl_catalog": work["id"]},
            "proposals": {"identifiers": {"bl_catalog": work["id"]}},
        })
    controller.catalog_version = "2026.10.22.1"
    qt = {
        name: getattr(module, name)
        for module in (QtCore, QtGui, QtWidgets)
        for name in dir(module)
        if name.startswith("Q") or name == "pyqtSignal"
    }
    gui = QtWidgets.QWidget()
    gui.current_db = SimpleNamespace(new_api=object())
    dialog = None
    try:
        with (
            patch.object(plugin_dialog, "_import_qt", return_value=qt),
            patch.object(plugin_dialog, "CalibreDbWriter", return_value=object()),
            patch.object(plugin_dialog, "DialogController", return_value=controller),
            patch.object(plugin_dialog, "update_prefs"),
        ):
            dialog = plugin_dialog.create_blc_dialog(gui, selected_ids=[1])
            dialog.show()
            dialog._after_scan()
            _settle(qt_app)
            yield dialog, controller
    finally:
        if dialog is not None:
            dialog.close()
        gui.close()
        qt_app.setFont(previous_font)
        _settle(qt_app)


def _assert_controls_fit(dialog):
    control_types = (
        QtWidgets.QPushButton, QtWidgets.QCheckBox, QtWidgets.QLineEdit,
        QtWidgets.QComboBox, QtWidgets.QSpinBox,
    )
    for control in dialog.findChildren(QtWidgets.QWidget):
        if not isinstance(control, control_types) or not control.isVisible():
            continue
        label = control.text() if isinstance(control, QtWidgets.QAbstractButton) else type(control).__name__
        assert control.width() >= control.minimumSizeHint().width(), label
        assert control.height() >= control.fontMetrics().height(), label
        if isinstance(control, QtWidgets.QAbstractButton):
            assert control.height() >= control.minimumSizeHint().height(), label
        parent = control.parentWidget()
        while parent is not None:
            bounds = QtCore.QRect(control.mapTo(parent, QtCore.QPoint()), control.size())
            assert parent.rect().contains(bounds), f"{label} clips in {type(parent).__name__}: {bounds}"
            if parent is dialog:
                break
            parent = parent.parentWidget()


@pytest.mark.parametrize("scanned_dialog", [(11, True), (12, False)], indirect=True)
def test_large_font_resize_round_trip_keeps_controls_and_saved_rows(qt_app, scanned_dialog):
    dialog, controller = scanned_dialog
    host_font = qt_app.font()
    for width, height in ((1240, 780), (1600, 1000), (1240, 780)):
        # Each target gets one resize: the old wide minimum must not clamp the return.
        dialog.resize(width, height)
        _settle(qt_app)
        assert dialog.size() == QtCore.QSize(width, height)
        _assert_controls_fit(dialog)
        assert dialog.table.rowCount() == 1881
        assert dialog._bucket_buttons["all"][0].text() == "All (1881)"
        assert dialog.apply_btn.text() == "Apply Visible Matches (1881)"
        assert dialog.table.horizontalScrollBar().maximum() == 0
        assert dialog.table.viewport().height() >= 6 * dialog.table.rowHeight(0)
        assert dialog.detail_view.viewport().height() >= 180
        assert not dialog._busy
        assert not dialog._elapsed_timer.isActive()

    # Host accessibility settings survive layout changes instead of being reduced to fit.
    for control in (dialog.chk_title, dialog.filter_edit, dialog.table):
        assert control.font().pointSizeF() == host_font.pointSizeF()
        assert control.font().bold() == host_font.bold()
    assert len(controller.rows) == 1881


@pytest.mark.parametrize("scanned_dialog", [(11, True), (12, False)], indirect=True)
def test_inspector_manual_tab_and_filters_remain_usable(qt_app, scanned_dialog):
    dialog, controller = scanned_dialog
    dialog.resize(1240, 780)
    dialog.table.selectRow(0)
    _settle(qt_app)
    assert "Book 1" in dialog.detail_view.toPlainText()
    assert dialog.detail_view.viewport().height() >= 180

    dialog.inspector_tabs.setCurrentIndex(1)
    dialog._show_candidates([controller.rows[0]["work"]])
    dialog.pick_list.setCurrentRow(0)
    _settle(qt_app)
    assert dialog.pick_search.isVisible()
    assert dialog.pick_list.isVisible()
    assert dialog.pick_list.viewport().height() >= 2 * dialog.pick_list.fontMetrics().height()
    assert dialog.pick_apply_btn.isEnabled()
    _assert_controls_fit(dialog)
    dialog.inspector_tabs.setCurrentIndex(0)
    _settle(qt_app)
    assert dialog.detail_view.isVisible()
    assert not dialog.pick_list.isVisible()

    dialog.filter_edit.setText("no fixture book has this title")
    _settle(qt_app)
    assert dialog.table.rowCount() == 0
    assert dialog.clear_filters_btn.isVisible()
    _assert_controls_fit(dialog)
    dialog.clear_filters_btn.click()
    _settle(qt_app)
    assert dialog.filter_edit.text() == ""
    assert dialog.table.rowCount() == 1881
    assert len(controller.rows) == 1881
    _assert_controls_fit(dialog)


def test_detail_proposals_are_separate_native_text_blocks(qt_app):
    row = {
        "id": 47, "title": "Virtues of the Sons", "authors": "Andy Smillie",
        "bucket": "certain_isbn", "add": {"bl_catalog": "w_fixture"},
        "proposals": {
            "identifiers": {"bl_catalog": "w_fixture"},
            "series": {"series": "The Horus Heresy", "series_index": 0},
            "synopsis": "Amit & Azkaellon <face> their past.",
            "tags": ["Short Story", "Blood Angels"],
            "cover": {"label": "Lexicanum cover · 1200×1800"},
        },
    }
    original = deepcopy(row)
    document = QtGui.QTextDocument()
    document.setHtml(render_work_detail_html(row))
    document.setTextWidth(280)
    expected = (
        "bl_catalog: w_fixture", "cover: Lexicanum cover · 1200×1800",
        "series: The Horus Heresy #0", "synopsis: Amit & Azkaellon <face> their past.",
        "tags: Short Story, Blood Angels",
    )
    blocks = [document.find(text).blockNumber() for text in expected]
    assert all(not document.find(text).isNull() for text in expected)
    assert len(set(blocks)) == len(expected)
    assert blocks == sorted(blocks)
    assert "{'series':" not in document.toPlainText()
    assert row == original


def test_selecting_external_cover_edit_refreshes_visible_proposal(qt_app, tmp_path):
    from test_calibre_cover_receipts import make_cover_controller

    controller, database, _lookups, _raw = make_cover_controller(tmp_path)
    assert controller.apply([1])[0]["status"] == "written"
    assert controller.rows[0]["cover_receipt"]
    qt = {
        name: getattr(module, name)
        for module in (QtCore, QtGui, QtWidgets)
        for name in dir(module)
        if name.startswith("Q") or name == "pyqtSignal"
    }
    gui = QtWidgets.QWidget()
    gui.current_db = SimpleNamespace(new_api=object())
    # Keep the saved rows but avoid starting a cache-loading worker in this UI test.
    controller.scan_cache = None
    with (
        patch.object(plugin_dialog, "_import_qt", return_value=qt),
        patch.object(plugin_dialog, "CalibreDbWriter", return_value=object()),
        patch.object(plugin_dialog, "DialogController", return_value=controller),
        patch.object(plugin_dialog, "update_prefs"),
    ):
        dialog = plugin_dialog.create_blc_dialog(gui, selected_ids=[1])
        try:
            dialog.show()
            dialog._after_scan()
            _settle(qt_app)
            assert "+cover" not in dialog.table.item(0, 5).text()
            assert not dialog.apply_btn.isEnabled()
            database.covers[1] = b"a replacement made outside the plugin"
            reads = len(database.cover_reads)
            dialog.table.selectRow(0)
            _settle(qt_app)
            assert database.cover_reads[reads:] == [1]
            assert "cover_receipt" not in controller.rows[0]
            assert "+cover" in dialog.table.item(0, 5).text()
            assert "cover" in dialog.table.item(0, 5).toolTip()
            assert "cover:" in dialog.detail_view.toPlainText()
            assert dialog.apply_btn.isEnabled()
            assert not dialog._busy
        finally:
            dialog.close()
            gui.close()

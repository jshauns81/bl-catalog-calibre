"""Optional Qt regression checks for refreshing a fully scanned Calibre library."""

from __future__ import annotations

import os
import sys
import threading
import time
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


@pytest.fixture(scope="module")
def qt_app():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def test_title_toggle_refreshes_1881_rows_and_preserves_apply_scope(qt_app):
    app = qt_app
    qt = {
        name: getattr(module, name)
        for module in (QtCore, QtGui, QtWidgets)
        for name in dir(module)
        if name.startswith("Q") or name == "pyqtSignal"
    }
    controller = plugin_dialog.DialogController(object(), prefs=dict(DEFAULTS))
    rows = []
    for bid in range(1, 1882):
        controller.books_by_id[bid] = {
            "id": bid, "title": f"Book {bid}", "authors": ["Fixture Author"], "identifiers": {},
        }
        rows.append({
            "id": bid, "title": f"Book {bid}", "authors": "Fixture Author",
            "bucket": "certain_title_author", "work_id": f"w_{bid}",
            "work": {
                "id": f"w_{bid}", "title": {"value": f"Canonical Book {bid}"},
                "authors": [{"name": "Fixture Author", "role": "author", "score": 1.0}],
            },
            "add": {"bl_catalog": f"w_{bid}"},
            "proposals": {"identifiers": {"bl_catalog": f"w_{bid}"}},
        })

    gui = QtWidgets.QWidget()
    gui.current_db = type("Database", (), {"new_api": object()})()
    with (
        patch.object(plugin_dialog, "_import_qt", return_value=qt),
        patch.object(plugin_dialog, "CalibreDbWriter", return_value=object()),
        patch.object(plugin_dialog, "DialogController", return_value=controller),
        patch.object(plugin_dialog, "update_prefs") as save,
    ):
        dialog = plugin_dialog.create_blc_dialog(gui)
        try:
            dialog.show()
            app.processEvents()
            authors = "; ".join(f"Author {i}" for i in range(100))
            rows[0]["authors"] = authors
            controller.rows = rows
            dialog._populate_table()
            # Realize the populated view: the stalled path only occurs on a later refresh.
            app.processEvents()
            assert dialog.table.item(0, 2).text() == "Author 0; Author 1; … (+98)"
            assert dialog.table.item(0, 2).toolTip() == authors
            assert not dialog.table.wordWrap()
            assert dialog.table.isColumnHidden(4) and dialog.table.isColumnHidden(6)
            assert dialog.table.horizontalScrollBar().maximum() == 0
            resize_started = time.monotonic()
            for i in range(80):
                dialog.resize(1240 + (i % 8) * 25, 780 + (i % 3) * 10)
                dialog.move(i % 10, i % 15)
                app.processEvents()
            assert time.monotonic() - resize_started < 5.0
            dialog.resize(1240, 780)
            app.processEvents()
            assert dialog.table.horizontalScrollBar().maximum() == 0
            dialog.details_columns.setChecked(True)
            assert not dialog.table.isColumnHidden(4) and not dialog.table.isColumnHidden(6)
            dialog.details_columns.setChecked(False)
            header = dialog.table.horizontalHeader()
            assert all(header.sectionResizeMode(i) != QtWidgets.QHeaderView.ResizeMode.ResizeToContents
                       for i in range(dialog.table.columnCount()))
            modes = [header.sectionResizeMode(i) for i in range(dialog.table.columnCount())]
            original_cells = [dialog.table.item(0, i) for i in range(dialog.table.columnCount())]
            start = time.monotonic()
            dialog.chk_title.click()
            elapsed = time.monotonic() - start
            assert "update_channel" not in save.call_args.args[0]
            assert elapsed < 5.0, f"Title toggle blocked for {elapsed:.2f}s"
            assert dialog.table.rowCount() == 1881
            assert dialog._selected_bids() == set()
            assert dialog.apply_btn.text() == "Apply Visible Matches (1881)"
            assert rows[0]["proposals"]["title"] == "Canonical Book 1"
            assert "+title" in dialog.table.item(0, 5).text()
            assert all(dialog.table.item(0, i) is item for i, item in enumerate(original_cells))
            assert modes == [header.sectionResizeMode(i) for i in range(dialog.table.columnCount())]
            assert dialog.table.updatesEnabled()
            assert not dialog.table.signalsBlocked()

            for row in (0, 900, 1880):
                for col in range(dialog.table.columnCount()):
                    dialog.table.item(row, col).setSelected(True)
            selection = dialog._selected_bids()
            assert selection == {1, 901, 1881}
            dialog.chk_title.click()
            assert dialog._selected_bids() == selection
            assert dialog.apply_btn.text() == "Apply Selected (3)"
            assert "title" not in rows[0]["proposals"]
            assert controller.plan_apply(selection)[0] == [1, 901, 1881]
            dialog.chk_identifiers.click()
            assert rows[0]["add"] == {}
            assert dialog.table.item(0, 5).text() == "No changes"
            dialog.chk_identifiers.click()
            assert rows[0]["add"] == {"bl_catalog": "w_1"}
            assert dialog._selected_bids() == selection

            dialog.filter_edit.setText("Canonical Book")
            assert dialog.table.rowCount() == 0
            assert dialog._selected_bids() == set()
            assert dialog.table_empty_lbl.isVisible()
            dialog.filter_edit.clear()
            assert dialog.table.rowCount() == 1881
            assert dialog._selected_bids() == set()
        finally:
            dialog.close()
            gui.close()


def test_update_dialog_checks_downloads_installs_and_recovers(qt_app):
    from bl_catalog_calibre import update_ui

    qt = {
        name: getattr(module, name)
        for module in (QtCore, QtGui, QtWidgets)
        for name in dir(module)
        if name.startswith("Q") or name == "pyqtSignal"
    }
    release = SimpleNamespace(tag="v1.2.0", notes="Fixture <b>release notes</b>")
    gate = threading.Event()

    def check(channel, **kwargs):
        assert gate.wait(3)
        return release

    def finish(dialog):
        deadline = time.monotonic() + 3
        while dialog._busy and time.monotonic() < deadline:
            qt_app.processEvents()
            QtCore.QThread.msleep(2)
        assert not dialog._busy
        qt_app.processEvents()

    with (
        patch.object(update_ui, "_import_qt", return_value=qt),
        patch.object(update_ui, "get_prefs", return_value={"update_channel": "stable"}),
        patch.object(update_ui, "update_prefs") as save,
        patch.object(update_ui, "check_for_update", side_effect=check) as checker,
        patch.object(update_ui, "download_update", return_value=b"verified fixture") as downloader,
        patch.object(update_ui, "install_downloaded_update") as installer,
        patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.No) as question,
    ):
        dialog = update_ui.create_update_dialog()
        try:
            dialog.show()
            qt_app.processEvents()
            assert not dialog.install_button.isEnabled()
            dialog.channel.setCurrentIndex(1)
            save.assert_called_with({"update_channel": "beta"})
            dialog.check_button.click()
            assert dialog._busy and not dialog.check_button.isEnabled()
            # A queued timer must fire while the network worker is deliberately held.
            heartbeat = []
            QtCore.QTimer.singleShot(0, lambda: heartbeat.append(True))
            qt_app.processEvents()
            assert heartbeat
            dialog.reject()
            assert dialog.isVisible()
            gate.set()
            finish(dialog)
            checker.assert_called_with("beta", calibre_version=None)
            assert dialog.install_button.isEnabled()
            assert dialog.notes.toPlainText() == release.notes
            assert "&lt;b&gt;" in dialog.notes.toHtml()
            dialog.install_button.click()
            downloader.assert_not_called()
            question.return_value = QtWidgets.QMessageBox.StandardButton.Yes
            dialog.install_button.click()
            finish(dialog)
            downloader.assert_called_once_with(release)
            installer.assert_called_once_with(release, b"verified fixture")
            assert dialog._restart_required
            assert "Restart Calibre" in dialog.status.text()
            assert not dialog.check_button.isEnabled()
            assert not dialog.install_button.isEnabled()
            dialog.close_button.click()
            assert not dialog.isVisible()
        finally:
            dialog.close()

        checker.side_effect = RuntimeError("Fixture connection failure")
        dialog = update_ui.create_update_dialog()
        try:
            dialog.show()
            dialog.check_button.click()
            finish(dialog)
            assert "Update failed" in dialog.status.text()
            assert dialog.check_button.isEnabled()
            assert "Fixture connection failure" in dialog.notes.toPlainText()
            checker.side_effect = None
            checker.return_value = None
            dialog.check_button.click()
            finish(dialog)
            assert "Up to date" in dialog.status.text()
            assert not dialog.install_button.isEnabled()
        finally:
            dialog.close()


def test_saved_library_is_automatically_ready_without_scan_click(qt_app, tmp_path):
    from bl_catalog_calibre.scan_cache import ScanCache
    from test_calibre_scan_cache import Catalogue, Library

    library, catalogue = Library(), Catalogue()
    controller = plugin_dialog.DialogController(
        library, prefs=dict(DEFAULTS), lookups=catalogue,
        scan_cache=ScanCache(tmp_path / "scan.sqlite", "test-library"),
    )
    controller.scan()
    controller.rows = []
    # Opening saved results must not silently start matching an unscanned book.
    library.books.append({**library.books[0], "id": 2, "title": "Unscanned"})
    catalogue.fail = True
    qt = {
        name: getattr(module, name)
        for module in (QtCore, QtGui, QtWidgets)
        for name in dir(module)
        if name.startswith("Q") or name == "pyqtSignal"
    }
    gui = QtWidgets.QWidget()
    gui.current_db = SimpleNamespace(new_api=object())
    with (
        patch.object(plugin_dialog, "_import_qt", return_value=qt),
        patch.object(plugin_dialog, "CalibreDbWriter", return_value=library),
        patch.object(plugin_dialog, "DialogController", return_value=controller),
    ):
        dialog = plugin_dialog.create_blc_dialog(gui)
        try:
            deadline = time.monotonic() + 5
            while dialog._busy and time.monotonic() < deadline:
                qt_app.processEvents()
                time.sleep(0.005)
            assert not dialog._busy
            assert dialog.table.rowCount() == 1
            assert "matches ready" in dialog.hero_state.text()
            assert not controller.cache_needs_check
            assert not dialog.pick_apply_btn.isEnabled()
            assert dialog.apply_btn.isEnabled()
            assert "Snapshot updates are manual" in dialog.hero_sub.text()
            assert catalogue.calls == 1
        finally:
            dialog.close()
            gui.close()


def test_apply_failure_shows_book_error_and_unattempted_count(qt_app):
    qt = {
        name: getattr(module, name)
        for module in (QtCore, QtGui, QtWidgets)
        for name in dir(module)
        if name.startswith("Q") or name == "pyqtSignal"
    }
    controller = plugin_dialog.DialogController(object(), prefs=dict(DEFAULTS))
    gui = QtWidgets.QWidget()
    gui.current_db = SimpleNamespace(new_api=object())
    with (
        patch.object(plugin_dialog, "_import_qt", return_value=qt),
        patch.object(plugin_dialog, "CalibreDbWriter", return_value=object()),
        patch.object(plugin_dialog, "DialogController", return_value=controller),
        patch.object(QtWidgets.QMessageBox, "warning") as warning,
    ):
        dialog = plugin_dialog.create_blc_dialog(gui)
        try:
            dialog._apply_target_count = 14
            dialog._after_apply([
                {"id": 2, "status": "written"}, {"id": 3, "status": "written"},
                {"id": 4, "title": "Path of the Dark Eldar", "status": "failed",
                 "error": "read-back: comments differs from what was sent", "rolled_back": True},
            ])
            assert "11 not attempted" in dialog.hero_state.text()
            message = warning.call_args.args[2]
            assert "book 4: Path of the Dark Eldar" in message
            assert "rolled back" in message
            assert "read-back: comments" in message
        finally:
            dialog.close()
            gui.close()


def test_apply_refresh_and_clear_filters_without_rescan(qt_app, tmp_path):
    from test_calibre_apply_refresh import make_controller

    controller, _database, catalogue = make_controller(tmp_path)
    qt = {
        name: getattr(module, name)
        for module in (QtCore, QtGui, QtWidgets)
        for name in dir(module)
        if name.startswith("Q") or name == "pyqtSignal"
    }
    gui = QtWidgets.QWidget()
    gui.current_db = SimpleNamespace(new_api=object())
    # Cache restoration is tested separately; start from the completed scan here.
    cache = controller.scan_cache
    controller.scan_cache = None
    with (
        patch.object(plugin_dialog, "_import_qt", return_value=qt),
        patch.object(plugin_dialog, "CalibreDbWriter", return_value=controller.writer),
        patch.object(plugin_dialog, "DialogController", return_value=controller),
        patch.object(plugin_dialog, "update_prefs"),
        patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.Yes),
    ):
        dialog = plugin_dialog.create_blc_dialog(gui)
        controller.scan_cache = cache
        try:
            dialog._after_scan()
            dialog.filter_edit.setText("Test")
            dialog._on_apply()
            deadline = time.monotonic() + 5
            while dialog._busy and time.monotonic() < deadline:
                qt_app.processEvents()
                time.sleep(0.005)
            assert not dialog._busy
            assert controller.rows[0]["bucket"] == "already"
            assert dialog.table.item(0, 3).text() == "Already stamped"
            assert dialog.table.item(0, 5).text() == "Updated · No changes"
            assert "Fields written: identifiers" in dialog.table.item(0, 5).toolTip()
            assert "Already (1)" == dialog._bucket_buttons["already"][0].text()
            assert not dialog.apply_btn.isEnabled()
            catalogue.fail = True
            dialog.hide_stamped_chk.setChecked(True)
            assert dialog.table.rowCount() == 0
            dialog._set_bucket("already")
            dialog.clear_filters_btn.click()
            assert dialog.table.rowCount() == 2
            assert not dialog.filter_edit.text()
            assert not dialog.hide_stamped_chk.isChecked()
            assert dialog._bucket_buttons["all"][0].isChecked()
        finally:
            dialog.close()
            gui.close()


@pytest.mark.parametrize("scenario", ["version_changed", "offline", "edited", "deleted"])
def test_saved_matches_do_not_poll_download_or_rematch_on_open(qt_app, tmp_path, scenario):
    from bl_catalog_calibre.scan_cache import ScanCache
    from test_calibre_scan_cache import Catalogue, Library

    library, catalogue = Library(), Catalogue()
    controller = plugin_dialog.DialogController(
        library, prefs=dict(DEFAULTS), lookups=catalogue,
        scan_cache=ScanCache(tmp_path / "scan.sqlite", "test-library"),
    )
    controller.scan()
    controller.rows = []
    if scenario == "version_changed":
        catalogue.version = "new"
    elif scenario == "edited":
        library.books[0]["tags"] = ["Edited"]
    elif scenario == "deleted":
        library.books.clear()
    catalogue.fail = True
    controller.build_lookups = lambda: (_ for _ in ()).throw(AssertionError("Unexpected lookup on open"))
    qt = {
        name: getattr(module, name)
        for module in (QtCore, QtGui, QtWidgets)
        for name in dir(module)
        if name.startswith("Q") or name == "pyqtSignal"
    }
    gui = QtWidgets.QWidget()
    gui.current_db = SimpleNamespace(new_api=object())
    with (
        patch.object(plugin_dialog, "_import_qt", return_value=qt),
        patch.object(plugin_dialog, "CalibreDbWriter", return_value=library),
        patch.object(plugin_dialog, "DialogController", return_value=controller),
        patch.object(QtWidgets.QMessageBox, "warning") as warning,
    ):
        dialog = plugin_dialog.create_blc_dialog(gui)
        try:
            deadline = time.monotonic() + 5
            while dialog._busy and time.monotonic() < deadline:
                qt_app.processEvents()
                time.sleep(0.005)
            assert not dialog._busy
            assert not warning.called
            assert catalogue.calls == 1
            unchanged = scenario in ("version_changed", "offline")
            assert dialog.table.rowCount() == (1 if unchanged else 0)
            assert dialog.apply_btn.isEnabled() == unchanged
            if scenario == "version_changed":
                assert dialog.table.item(0, 3).text().startswith("Saved · ")
                assert "Saved match catalogue: one" in dialog.table.item(0, 5).toolTip()
        finally:
            dialog.close()
            gui.close()


def test_sync_requires_confirmation_and_keeps_saved_apply_ready(qt_app, tmp_path):
    from test_calibre_apply_refresh import make_controller

    controller, _database, catalogue = make_controller(tmp_path)
    qt = {
        name: getattr(module, name)
        for module in (QtCore, QtGui, QtWidgets)
        for name in dir(module)
        if name.startswith("Q") or name == "pyqtSignal"
    }
    gui = QtWidgets.QWidget()
    gui.current_db = SimpleNamespace(new_api=object())
    downloads = []
    def sync():
        downloads.append(True)
        controller.catalog_version = "two"
        return tmp_path / "snapshot.sqlite", "two"
    controller.sync_snapshot = sync
    with (
        patch.object(plugin_dialog, "_import_qt", return_value=qt),
        patch.object(plugin_dialog, "CalibreDbWriter", return_value=controller.writer),
        patch.object(plugin_dialog, "DialogController", return_value=controller),
        patch.object(plugin_dialog, "update_prefs"),
        patch.object(QtWidgets.QMessageBox, "question") as question,
    ):
        dialog = plugin_dialog.create_blc_dialog(gui)
        try:
            deadline = time.monotonic() + 5
            while dialog._busy and time.monotonic() < deadline:
                qt_app.processEvents()
                time.sleep(0.005)
            before = controller.scan_cache.load()
            calls = catalogue.calls
            question.return_value = QtWidgets.QMessageBox.StandardButton.No
            dialog._on_sync_snapshot()
            assert not downloads and not dialog._busy
            question.return_value = QtWidgets.QMessageBox.StandardButton.Yes
            dialog._on_sync_snapshot()
            while dialog._busy and time.monotonic() < deadline:
                qt_app.processEvents()
                time.sleep(0.005)
            assert not dialog._busy
            assert len(downloads) == 1
            assert dialog.apply_btn.isEnabled()
            assert catalogue.calls == calls
            assert controller.scan_cache.load() == before
            assert "saved matches kept" in dialog.hero_state.text()
        finally:
            dialog.close()
            gui.close()

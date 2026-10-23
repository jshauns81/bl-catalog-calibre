"""Optional Qt integration checks for working in Calibre beside the matcher."""

from __future__ import annotations

import gc
import os
import sys
import threading
import time
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")
from PyQt6 import QtCore, QtGui, QtTest, QtWidgets

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations" / "calibre"))
from bl_catalog_calibre import action as plugin_action
from bl_catalog_calibre import dialog as plugin_dialog
from bl_catalog_calibre.config import DEFAULTS


class LibraryModel(QtGui.QStandardItemModel):
    def id(self, index):
        row = index.row() if hasattr(index, "row") else index
        return self.item(row).data(QtCore.Qt.ItemDataRole.UserRole)


class LibraryView(QtWidgets.QTableView):
    def __init__(self):
        super().__init__()
        self.setModel(LibraryModel())
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.selections = []
        self.set_ids([21, 8, 45])

    def set_ids(self, ids):
        self.model().clear()
        for bid in ids:
            item = QtGui.QStandardItem(f"Book {bid}")
            item.setData(bid, QtCore.Qt.ItemDataRole.UserRole)
            self.model().appendRow(item)

    def ids_to_rows(self, ids):
        return {self.model().id(row): row for row in range(self.model().rowCount())
                if self.model().id(row) in ids}

    @property
    def current_id(self):
        index = self.currentIndex()
        return self.model().id(index) if index.isValid() else None

    def get_selected_ids(self):
        return [self.model().id(index) for index in self.selectionModel().selectedRows()]

    def select_rows(self, ids, using_ids=True):
        assert using_ids
        self.selections.append(list(ids))
        rows = self.ids_to_rows(ids)
        self.selectionModel().clearSelection()
        if ids:
            self.setCurrentIndex(self.model().index(rows[ids[0]], 0))
        for bid in ids:
            self.selectionModel().select(self.model().index(rows[bid], 0), QtCore.QItemSelectionModel.SelectionFlag.Select
                                         | QtCore.QItemSelectionModel.SelectionFlag.Rows)


@pytest.fixture
def host():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    qt = {name: getattr(module, name) for module in (QtCore, QtGui, QtWidgets)
          for name in dir(module) if name.startswith("Q") or name == "pyqtSignal"}
    gui = QtWidgets.QMainWindow()
    gui.current_db = SimpleNamespace(new_api=object())
    gui.library_view = LibraryView()
    gui.setCentralWidget(gui.library_view)
    gui.show()
    controller = plugin_dialog.DialogController(object(), prefs=dict(DEFAULTS))
    for bid in (8, 21, 45):
        controller.rows.append({"id": bid, "title": f"Book {bid}", "authors": "Author",
                                "bucket": "conflict", "work_id": "", "proposals": {}})
    writer = Mock()
    action = plugin_action.BLCatalogAction()
    action.gui = gui
    with (
        patch.object(plugin_dialog, "_import_qt", return_value=qt),
        patch.object(plugin_dialog, "CalibreDbWriter", return_value=writer),
        patch.object(plugin_dialog, "DialogController", return_value=controller),
        patch.object(plugin_dialog, "update_prefs"),
        patch.dict(sys.modules, {"qt.core": SimpleNamespace(QTimer=QtCore.QTimer)}),
    ):
        yield app, gui, action, controller, writer
        action._retire_dialog()
        gui.close()
        app.processEvents()


def finish(app, dialog):
    deadline = time.monotonic() + 3
    while dialog._busy and time.monotonic() < deadline:
        app.processEvents()
        QtCore.QThread.msleep(2)
    assert not dialog._busy
    app.processEvents()


def test_toolbar_opens_one_independent_nonmodal_window(host):
    app, gui, action, _controller, _writer = host
    dialog = action.show_dialog()
    app.processEvents()
    assert gui.isEnabled() and gui.library_view.isEnabled()
    assert app.activeModalWidget() is None
    assert dialog.parentWidget() is None
    assert dialog.windowType() == QtCore.Qt.WindowType.Window
    assert dialog.windowModality() == QtCore.Qt.WindowModality.NonModal
    assert not (dialog.windowFlags() & QtCore.Qt.WindowType.WindowStaysOnTopHint)
    dialog.showMinimized()
    assert action.show_dialog() is dialog
    assert not dialog.isMinimized()
    dialog.close()
    assert action._dialog is None
    assert action.show_dialog() is not dialog


def test_mouse_keyboard_and_show_button_select_book_ids_without_changing_apply_scope(host):
    app, gui, action, _controller, _writer = host
    dialog = action.show_dialog()
    app.processEvents()
    cell = dialog.table.visualItemRect(dialog.table.item(0, 1)).center()
    QtTest.QTest.mouseClick(dialog.table.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=cell)
    assert gui.library_view.model().id(gui.library_view.currentIndex()) == 8
    assert gui.library_view.selections == [[8]]
    assert dialog.scan_sel_btn.text() == "Scan Selected (1)"
    QtTest.QTest.keyClick(dialog.table, QtCore.Qt.Key.Key_Down)
    assert gui.library_view.model().id(gui.library_view.currentIndex()) == 21
    assert "21 selected" in dialog.calibre_link_status.text()
    dialog.table.item(2, 0).setSelected(True)
    selected = dialog._selected_bids()
    gui.library_view.select_rows([8])
    dialog.show_calibre_btn.click()
    assert gui.library_view.model().id(gui.library_view.currentIndex()) == 21
    assert dialog._selected_bids() == selected
    gui.library_view.selections.clear()
    dialog._populate_table()
    assert not gui.library_view.selections
    assert dialog._selected_bids() == selected


def test_hidden_book_keeps_calibre_filter_and_selection(host):
    app, gui, action, _controller, _writer = host
    dialog = action.show_dialog()
    app.processEvents()
    gui.library_view.set_ids([45])
    gui.library_view.select_rows([45])
    gui.library_view.selections.clear()
    dialog.table.setCurrentCell(0, 0)
    assert "hidden or removed" in dialog.calibre_link_status.text()
    assert not gui.library_view.selections
    assert gui.library_view.model().rowCount() == 1
    assert gui.library_view.model().id(gui.library_view.currentIndex()) == 45


def test_live_calibre_selection_is_used_for_scan_selected(host):
    _app, gui, action, controller, _writer = host
    dialog = action.show_dialog()
    gui.library_view.select_rows([45, 8])
    assert dialog.scan_sel_btn.text() == "Scan Selected (2)"
    with patch.object(dialog, "_start_job") as start, patch.object(controller, "scan") as scan:
        dialog._run_scan(selected_only=True)
        start.call_args.args[1]()
    assert set(scan.call_args.args[0]) == {45, 8}


def test_library_switch_retires_worker_before_next_library_and_skips_callback(host):
    app, gui, action, _controller, writer = host
    dialog = action.show_dialog()
    gate = threading.Event()
    done = Mock()
    dialog._start_job("Fixture", lambda: gate.wait(3), done, cancellable=True)
    old_db = gui.current_db
    action.library_about_to_change(old_db, object())
    assert dialog._cancel.is_set() and dialog._retired and not dialog.isVisible()
    writer.detach.assert_called_once()
    gui.current_db = SimpleNamespace(new_api=object())
    next_dialog = action.show_dialog()
    assert next_dialog is not dialog
    gate.set()
    finish(app, dialog)
    done.assert_not_called()
    assert action._dialog is next_dialog


def test_snapshot_menu_reuses_window_and_stops_waiting_when_closed(host):
    app, _gui, action, _controller, _writer = host
    dialog = action.show_dialog()
    with patch.object(dialog, "_on_sync_snapshot") as sync:
        action.sync_snapshot_now()
        app.processEvents()
        sync.assert_called_once()
        action.sync_snapshot_now()
        dialog.close()
        app.processEvents()
        sync.assert_called_once()


def test_snapshot_menu_coalesces_requests_while_a_worker_runs(host):
    app, _gui, action, _controller, _writer = host
    dialog = action.show_dialog()
    gate = threading.Event()
    dialog._start_job("Fixture", lambda: gate.wait(3), lambda _value: None, cancellable=True)
    with patch.object(dialog, "_on_sync_snapshot") as sync:
        action.sync_snapshot_now()
        action.sync_snapshot_now()
        app.processEvents()
        sync.assert_not_called()
        gate.set()
        finish(app, dialog)
        QtTest.QTest.qWait(150)
        sync.assert_called_once()


def test_retired_window_keeps_worker_bridge_alive_without_action_reference(host):
    app, _gui, action, _controller, _writer = host
    dialog = action.show_dialog()
    gate = threading.Event()
    dialog._start_job("Fixture", lambda: gate.wait(3), Mock(), cancellable=True)
    reference = weakref.ref(dialog)
    action._retire_dialog()
    del dialog
    gc.collect()
    assert reference() is not None
    assert reference()._busy
    gate.set()
    finish(app, reference())


def test_shutdown_retires_independent_window(host):
    _app, _gui, action, _controller, writer = host
    dialog = action.show_dialog()
    action.shutting_down()
    assert action._dialog is None and not dialog.isVisible()
    writer.detach.assert_called_once()

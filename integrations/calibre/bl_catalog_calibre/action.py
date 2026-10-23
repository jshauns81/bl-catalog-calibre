"""Calibre GUI InterfaceAction for BL-Catalog.

Adds the BL-Catalog toolbar action (using the warm brass book-and-skull icon from
``images/icon.png``) with a dropdown menu for opening the interactive Reading Lamp
dialog, syncing the local SQLite snapshot, or configuring preferences.
"""

from __future__ import annotations

from .dialog import create_blc_dialog

try:
    from calibre.gui2.actions import InterfaceAction as CalibreInterfaceAction  # type: ignore[import-not-found]
except ImportError:

    class CalibreInterfaceAction:  # type: ignore[no-redef]
        name = "BL-Catalog"


def _load_plugin_icon():
    """Load ``images/icon.png`` from the plugin ZIP.

    Calibre's zip plugin loader injects ``get_icons`` into the globals of every module it loads
    from the plugin archive; ``calibre.gui2`` does not export it. Outside Calibre the name is
    absent and the action simply has no icon.
    """
    loader = globals().get("get_icons")
    if loader is None:
        return None
    try:
        icon = loader("images/icon.png", "BL-Catalog")
    except (OSError, KeyError, TypeError, ValueError):
        return None
    if icon is None or (hasattr(icon, "isNull") and icon.isNull()):
        return None
    return icon


class BLCatalogAction(CalibreInterfaceAction):
    """Toolbar action opening the Reading Lamp BL-Catalog matcher & review dialog."""

    name = "BL-Catalog"
    action_spec = (
        "BL-Catalog",
        None,
        "Match books, identifiers, metadata, and covers against BL-Catalog",
        None,
    )
    action_type = "current"
    action_add_menu = True
    action_menu_clone_qaction = "Open BL-Catalog Matcher…"

    _dialog = None
    _sync_dialog = None

    def genesis(self) -> None:
        icon = _load_plugin_icon()
        if icon is not None:
            self.qaction.setIcon(icon)
        menu = self.qaction.menu()
        if menu is not None:
            self.create_menu_action(
                menu,
                "blc_sync_snapshot",
                "Sync Local BLC Snapshot",
                icon=icon,
                triggered=self.sync_snapshot_now,
            )
            menu.addSeparator()
            self.create_menu_action(
                menu,
                "blc_check_updates",
                "Check for Plugin Updates…",
                triggered=self.check_plugin_updates,
            )
            self.create_menu_action(
                menu,
                "blc_configure",
                "Configure BL-Catalog…",
                triggered=self.show_configuration,
            )
        self.qaction.triggered.connect(self.show_dialog)
        if hasattr(self.gui, "shutdown_started"):
            self.gui.shutdown_started.connect(self._retire_dialog)

    def _selected_book_ids(self) -> list[int]:
        try:
            rows = self.gui.library_view.selectionModel().selectedRows()
            if not rows:
                return []
            model = self.gui.library_view.model()
            return [int(model.id(r)) for r in rows]
        except (AttributeError, TypeError, ValueError):
            return []

    def show_dialog(self):
        dlg = self._dialog
        if dlg is None:
            dlg = create_blc_dialog(self.gui, icon=_load_plugin_icon(), selected_ids=self._selected_book_ids())
            self._dialog = dlg

            def released(_result) -> None:
                if self._dialog is dlg:
                    self._dialog = None
                dlg.deleteLater()

            dlg.finished.connect(released)
        dlg.refresh_calibre_selection()
        if dlg.isMinimized():
            dlg.showNormal()
        else:
            dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        return dlg

    def sync_snapshot_now(self) -> None:
        dlg = self.show_dialog()
        if self._sync_dialog is dlg:
            return
        self._sync_dialog = dlg
        # Use the dialog's confirmation and worker rather than downloading on the GUI thread.
        from qt.core import QTimer

        def sync_when_ready() -> None:
            if self._dialog is not dlg or not dlg.isVisible():
                if self._sync_dialog is dlg:
                    self._sync_dialog = None
                return
            if dlg._busy:
                QTimer.singleShot(100, sync_when_ready)
            else:
                self._sync_dialog = None
                dlg._on_sync_snapshot()

        QTimer.singleShot(0, sync_when_ready)

    def library_about_to_change(self, olddb, db) -> None:
        self._retire_dialog()

    def library_changed(self, db) -> None:
        self._retire_dialog()

    def shutting_down(self) -> None:
        self._retire_dialog()

    def _retire_dialog(self) -> None:
        dlg = self._dialog
        if dlg is not None:
            self._dialog = None
            # A running worker keeps its window/bridge alive until it has stopped.
            dlg.retire_library()

    def show_configuration(self) -> None:
        self.interface_action_base_plugin.do_user_config(self.gui)

    def check_plugin_updates(self) -> None:
        from .update_ui import create_update_dialog

        create_update_dialog(self.gui).exec()

    def apply_settings(self) -> None:
        pass

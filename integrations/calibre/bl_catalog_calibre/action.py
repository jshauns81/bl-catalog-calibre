"""Calibre GUI InterfaceAction for BL-Catalog.

Adds the BL-Catalog toolbar action (using the warm brass book-and-skull icon from
``images/icon.png``) with a dropdown menu for opening the interactive Reading Lamp
dialog, syncing the local SQLite snapshot, or configuring preferences.
"""

from __future__ import annotations

from .apply import CalibreDbWriter
from .dialog import DialogController, create_blc_dialog
from .matcher import CatalogueError

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

    def _selected_book_ids(self) -> list[int]:
        try:
            rows = self.gui.library_view.selectionModel().selectedRows()
            if not rows:
                return []
            model = self.gui.library_view.model()
            return [int(model.id(r)) for r in rows]
        except (AttributeError, TypeError, ValueError):
            return []

    def show_dialog(self) -> None:
        icon = _load_plugin_icon()
        selected_ids = self._selected_book_ids()
        dlg = create_blc_dialog(self.gui, icon=icon, selected_ids=selected_ids)
        dlg.exec()

    def sync_snapshot_now(self) -> None:
        try:
            from calibre.gui2 import error_dialog, info_dialog  # type: ignore[import-not-found]
        except ImportError:
            info_dialog = error_dialog = None
        try:
            writer = CalibreDbWriter(self.gui.current_db.new_api)
            ctl = DialogController(writer)
            target, version = ctl.sync_snapshot()
            if info_dialog is not None:
                info_dialog(
                    self.gui,
                    "BL-Catalog Snapshot Synced",
                    f"Downloaded snapshot {version} to {target}",
                    show=True,
                )
        except (CatalogueError, OSError, ValueError) as exc:
            if error_dialog is not None:
                error_dialog(
                    self.gui,
                    "BL-Catalog Snapshot Error",
                    str(exc),
                    show=True,
                )

    def show_configuration(self) -> None:
        self.interface_action_base_plugin.do_user_config(self.gui)

    def check_plugin_updates(self) -> None:
        from .update_ui import create_update_dialog

        create_update_dialog(self.gui).exec()

    def apply_settings(self) -> None:
        pass

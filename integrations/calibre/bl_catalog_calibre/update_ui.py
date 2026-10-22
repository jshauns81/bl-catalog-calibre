"""Non-blocking plugin update dialog using Calibre's installer."""

from __future__ import annotations

import threading

from .config import get_prefs, update_prefs
from .dialog import _import_qt
from .style import build_stylesheet
from .updates import check_for_update, download_update, install_downloaded_update
from .version import RELEASE_TAG


def create_update_dialog(parent=None):
    qt = _import_qt()
    if qt is None:
        raise RuntimeError("Qt is required to check for plugin updates.")
    try:
        from calibre.constants import numeric_version  # type: ignore[import-not-found]
    except ImportError:
        numeric_version = None

    class Bridge(qt["QObject"]):
        finished = qt["pyqtSignal"](object)

    class UpdateDialog(qt["QDialog"]):
        def __init__(self):
            super().__init__(parent)
            self.setWindowTitle("BL-Catalog Plugin Updates")
            self.resize(580, 400)
            self.setStyleSheet(build_stylesheet())
            self._busy = False
            self._release = None
            self._restart_required = False
            self._bridge = Bridge(self)
            self._bridge.finished.connect(self._finished)
            layout = qt["QVBoxLayout"](self)
            self.installed = qt["QLabel"](f"Installed plugin: {RELEASE_TAG}")
            self.installed.setObjectName("MutedLabel")
            layout.addWidget(self.installed)
            channel_row = qt["QHBoxLayout"]()
            channel_label = qt["QLabel"]("Update channel:")
            channel_label.setObjectName("MutedLabel")
            channel_row.addWidget(channel_label)
            self.channel = qt["QComboBox"]()
            self.channel.addItem("Stable", "stable")
            self.channel.addItem("Beta (includes prereleases)", "beta")
            self.channel.setCurrentIndex(1 if get_prefs().get("update_channel") == "beta" else 0)
            self.channel.currentIndexChanged.connect(self._channel_changed)
            channel_row.addWidget(self.channel, 1)
            layout.addLayout(channel_row)
            self.status = qt["QLabel"]("Check GitHub for a newer plugin release.")
            self.status.setObjectName("MutedLabel")
            self.status.setWordWrap(True)
            layout.addWidget(self.status)
            self.notes = qt["QTextBrowser"]()
            self.notes.setPlainText("Release notes will appear here after an update check.")
            layout.addWidget(self.notes, 1)
            buttons = qt["QHBoxLayout"]()
            self.check_button = qt["QPushButton"]("Check for Updates")
            self.check_button.clicked.connect(self._check)
            buttons.addWidget(self.check_button)
            self.install_button = qt["QPushButton"]("Install Update")
            self.install_button.setObjectName("PrimaryButton")
            self.install_button.setEnabled(False)
            self.install_button.clicked.connect(self._confirm_install)
            buttons.addWidget(self.install_button)
            self.close_button = qt["QPushButton"]("Close")
            self.close_button.clicked.connect(self.reject)
            buttons.addWidget(self.close_button)
            layout.addLayout(buttons)

        def _channel_changed(self):
            update_prefs({"update_channel": self.channel.currentData()})
            self._release = None
            self.install_button.setEnabled(False)
            self.status.setText("Channel changed. Check GitHub for a newer release.")
            self.notes.setPlainText("Release notes will appear here after an update check.")

        def _start(self, kind, function):
            if self._busy:
                return
            self._busy = True
            self.check_button.setEnabled(False)
            self.install_button.setEnabled(False)
            self.channel.setEnabled(False)
            self.close_button.setEnabled(False)

            def run():
                try:
                    outcome = (kind, True, function())
                except Exception as exc:  # noqa: BLE001 - worker failures return to the GUI
                    outcome = (kind, False, str(exc) or type(exc).__name__)
                self._bridge.finished.emit(outcome)

            threading.Thread(target=run, name="blc-plugin-update", daemon=True).start()

        def _check(self):
            channel = self.channel.currentData()
            self._release = None
            self.status.setText("Checking GitHub for plugin updates…")
            self.notes.setPlainText("Fetching release information.")
            self._start("check", lambda: check_for_update(channel, calibre_version=numeric_version))

        def _confirm_install(self):
            if self._release is None or self._busy:
                return
            answer = qt["QMessageBox"].question(
                self, "Install BL-Catalog Update",
                f"Install {self._release.tag} from GitHub?\n\n"
                "You will need to restart Calibre to use the updated plugin.",
            )
            if answer != qt["QMessageBox"].StandardButton.Yes:
                return
            release = self._release
            self.status.setText(f"Downloading and verifying {release.tag}…")
            self._start("download", lambda: download_update(release))

        def _finished(self, outcome):
            kind, success, value = outcome
            if success and kind == "download":
                try:
                    install_downloaded_update(self._release, value)
                except Exception as exc:  # noqa: BLE001 - Calibre installer errors stay in the dialog
                    success, value = False, str(exc) or type(exc).__name__
                else:
                    self._restart_required = True
                    self.status.setText(f"Installed {self._release.tag}. Restart Calibre when ready.")
                    self.notes.setPlainText("Close this dialog, then quit and reopen Calibre to load the update.")
            self._busy = False
            self.close_button.setEnabled(True)
            self.channel.setEnabled(not self._restart_required)
            self.check_button.setEnabled(not self._restart_required)
            if not success:
                self._release = None
                self.status.setText("Update failed. You can check again to retry.")
                self.notes.setPlainText(str(value))
            elif kind == "check":
                self._release = value
                if value is None:
                    self.status.setText(f"Up to date on {self.channel.currentText()}.")
                    self.notes.setPlainText(f"Installed plugin: {RELEASE_TAG}\nNo newer release is available.")
                else:
                    self.status.setText(f"Update available: {value.tag}")
                    self.notes.setPlainText(value.notes)
                    self.install_button.setEnabled(True)

        def reject(self):
            if not self._busy:
                super().reject()

        def closeEvent(self, event):
            if self._busy:
                event.ignore()
            else:
                super().closeEvent(event)

    return UpdateDialog()

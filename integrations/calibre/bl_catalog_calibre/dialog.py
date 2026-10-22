"""Reading Lamp GUI dialog, state controller, and preferences widget for BL-Catalog in Calibre.

Separates pure-Python state and workflow logic (``DialogController``) from the Qt widget layer
(``BLCatalogDialog`` and ``BLCatalogConfigWidget``) so all scan, filter, manual work-pick,
CSV export, snapshot sync, and guarded apply flows can be tested headlessly.
"""

from __future__ import annotations

import collections
import contextlib
import threading
from pathlib import Path
from typing import Any

from .apply import ApplyRefused, CalibreDbWriter, apply_matches
from .client import HttpCatalogClient, Lookups, SnapshotCatalogClient
from .config import (
    ApplyOptions,
    current_apply_options,
    get_prefs,
    update_prefs,
)
from .matcher import (
    APPLICABLE_BUCKETS,
    BUCKETS,
    apply_manual_pick,
    authors_of,
    build_proposals,
    offered_credits,
    run_scan,
    title_of,
    write_csv,
)
from .style import BUCKET_STYLE, build_stylesheet, render_work_detail_html


class OperationCancelled(Exception):
    """Raised inside a background job when the user pressed Cancel."""


def is_applicable(row: dict[str, Any]) -> bool:
    """A scan row the Apply button may write: certain, picked, or already-linked with changes."""
    bucket = row.get("bucket")
    if bucket in APPLICABLE_BUCKETS:
        return True
    return bucket == "already" and bool(row.get("proposals"))


class DialogController:
    """Headless controller managing scan rows, bucket filters, manual work picks, and apply."""

    def __init__(
        self,
        writer: Any,
        *,
        prefs: dict[str, Any] | None = None,
        lookups: Lookups | None = None,
    ) -> None:
        self.writer = writer
        self.prefs = dict(prefs if prefs is not None else get_prefs())
        self.options: ApplyOptions = current_apply_options(self.prefs)
        self._injected_lookups = lookups
        self.books: list[dict[str, Any]] = []
        self.books_by_id: dict[int, dict[str, Any]] = {}
        self.rows: list[dict[str, Any]] = []
        self.bucket_filter: str = "all"
        self.search_filter: str = ""
        self.catalog_version: str | None = None

    def set_options(self, options: ApplyOptions) -> None:
        self.options = options
        self.prefs.update({
            "apply_identifiers": options.identifiers,
            "apply_title": options.title,
            "apply_authors": options.authors,
            "apply_series": options.series,
            "apply_synopsis": options.synopsis,
            "apply_tags": options.tags,
            "apply_cover": options.cover,
        })
        for row in self.rows:
            bid = int(row["id"])
            book = self.books_by_id.get(bid)
            work = row.get("work")
            if book and work and row["bucket"] in APPLICABLE_BUCKETS:
                picked = bool(row.get("picked"))
                proposals = build_proposals(book, work, options, picked=picked)
                row["proposals"] = proposals
                row["add"] = dict(proposals.get("identifiers") or {})
            elif book and work and row["bucket"] == "already":
                with_id = str(row.get("work_id") or "") != str(row.get("bl_catalog") or "")
                proposals = build_proposals(book, work, options, picked=False, with_id=with_id)
                row["proposals"] = proposals
                row["add"] = dict(proposals.get("identifiers") or {})

    def build_lookups(self) -> Lookups:
        if self._injected_lookups is not None:
            self.catalog_version = self._injected_lookups.version
            return self._injected_lookups
        http_client = HttpCatalogClient(
            self.prefs.get("url"),
            token=self.prefs.get("token"),
            timeout=float(self.prefs.get("timeout") or 10.0),
        )
        snap_path = self.prefs.get("snapshot_path")
        if self.prefs.get("mode") == "snapshot" and snap_path and Path(snap_path).is_file():
            snap_client = SnapshotCatalogClient(snap_path, http_client=http_client)
            self.catalog_version = snap_client.version
            return Lookups(snap_client)
        return Lookups(http_client)

    def sync_snapshot(self) -> tuple[Path, str]:
        """Download ``/v1/snapshots/latest.sqlite`` to the configured ``snapshot_path``."""
        http_client = HttpCatalogClient(
            self.prefs.get("url"),
            token=self.prefs.get("token"),
            timeout=float(self.prefs.get("timeout") or 10.0),
        )
        snap_path = self.prefs.get("snapshot_path") or "/tmp/bl-catalog-latest.sqlite"
        target, version = http_client.download_snapshot(snap_path)
        self.prefs["mode"] = "snapshot"
        self.prefs["snapshot_path"] = str(target)
        self.catalog_version = version
        self._injected_lookups = None
        return target, version

    def scan(
        self,
        book_ids: list[int] | tuple[int, ...] | None = None,
        progress=None,
    ) -> list[dict[str, Any]]:
        """Read books from ``writer`` and classify them into BLC buckets."""
        all_books = self.writer.read_books(None)
        self.books = all_books
        self.books_by_id = {int(b["id"]): b for b in all_books}
        if book_ids is not None:
            wanted = {int(i) for i in book_ids}
            target_books = [b for b in all_books if int(b["id"]) in wanted]
        else:
            target_books = all_books

        lookups = self.build_lookups()
        self.rows = run_scan(target_books, lookups, options=self.options, progress=progress)
        self.catalog_version = lookups.version or self.catalog_version
        return self.rows

    def bucket_counts(self) -> dict[str, int]:
        counts = collections.Counter(r["bucket"] for r in self.rows)
        out = {"all": len(self.rows)}
        for b in BUCKETS:
            out[b] = counts.get(b, 0)
        return out

    def filtered_rows(self) -> list[dict[str, Any]]:
        q = self.search_filter.strip().lower()
        out: list[dict[str, Any]] = []
        for row in self.rows:
            if self.bucket_filter != "all" and row.get("bucket") != self.bucket_filter:
                continue
            if q:
                haystack = " ".join(
                    str(row.get(k) or "")
                    for k in ("id", "title", "authors", "isbn", "work_id", "blc_title", "reason")
                ).lower()
                if q not in haystack:
                    continue
            out.append(row)
        return out

    def search_blc(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        lookups = self.build_lookups()
        results = lookups.search_discovery(query, limit=limit)
        self.catalog_version = lookups.version or self.catalog_version
        return results

    def pick_work_for_book(self, book_id: int, work: dict[str, Any]) -> dict[str, Any]:
        """Assign ``work`` as the owner-picked match for ``book_id`` (D077)."""
        book = self.books_by_id.get(int(book_id))
        if book is None:
            raise KeyError(f"Book {book_id} is not loaded")
        for row in self.rows:
            if int(row["id"]) == int(book_id):
                return apply_manual_pick(row, book, work, self.options)
        raise KeyError(f"Scan row for book {book_id} not found")

    def export_csv(self, path: str | Path) -> None:
        write_csv(self.rows, path)

    def plan_apply(
        self,
        selected_ids: list[int] | tuple[int, ...] | set[int] | None = None,
        *,
        max_books: int | None = None,
    ) -> tuple[list[int], int]:
        """Return ``(ids to write, eligible count)`` for an Apply click.

        A non-empty selection is used exactly as selected, even a single row. With no selection,
        only applicable rows visible under the current bucket and search filters are eligible,
        capped at ``max_books`` in display order. Hidden rows are never written.
        """
        cap = int(max_books if max_books is not None else (self.prefs.get("max_apply") or 50))
        if selected_ids:
            ids = sorted({int(i) for i in selected_ids})
            return ids, len(ids)
        eligible = [int(row["id"]) for row in self.filtered_rows() if is_applicable(row)]
        return eligible[:cap], len(eligible)

    def apply(
        self,
        book_ids: list[int] | None = None,
        *,
        max_books: int | None = None,
        log_path: str | Path | None = None,
        should_stop: Any = None,
    ) -> list[dict[str, Any]]:
        """Run guarded apply on exactly ``book_ids`` (default: ``plan_apply()`` with no selection)."""
        cap = int(max_books if max_books is not None else (self.prefs.get("max_apply") or 50))
        log = log_path if log_path is not None else self.prefs.get("log_path")
        if book_ids is None:
            book_ids, _eligible = self.plan_apply(None, max_books=cap)
        wanted = [int(i) for i in book_ids]
        if not wanted:
            raise ApplyRefused("No visible certain, picked, or updatable rows to apply")

        # Re-read the library so classification and duplicate checks use current Calibre state.
        all_books = self.writer.read_books(None)
        self.books = all_books
        self.books_by_id = {int(b["id"]): b for b in all_books}

        rows_by_id = {int(r["id"]): r for r in self.rows}
        targets: list[dict[str, Any]] = []
        for bid in wanted:
            row = rows_by_id.get(bid) or {}
            targets.append({
                "id": bid,
                "picked": bool(row.get("picked")),
                "work_id": row.get("work_id"),
                "picked_work_id": row.get("work_id") if row.get("picked") else None,
            })

        lookups = self.build_lookups()
        return apply_matches(
            targets,
            max_books=cap,
            books=all_books,
            lookups=lookups,
            writer=self.writer,
            log_path=log,
            options=self.options,
            should_stop=should_stop,
        )


def _import_qt():
    try:
        from qt.core import (  # type: ignore[import-not-found]
            QAbstractItemView,
            QCheckBox,
            QComboBox,
            QDialog,
            QFileDialog,
            QFrame,
            QGridLayout,
            QHBoxLayout,
            QHeaderView,
            QIcon,
            QLabel,
            QLineEdit,
            QListWidget,
            QListWidgetItem,
            QMessageBox,
            QObject,
            QPixmap,
            QProgressBar,
            QPushButton,
            QSpinBox,
            QSplitter,
            Qt,
            QTableWidget,
            QTableWidgetItem,
            QTextBrowser,
            QVBoxLayout,
            QWidget,
            pyqtSignal,
        )

        return {
            "QAbstractItemView": QAbstractItemView,
            "QCheckBox": QCheckBox,
            "QComboBox": QComboBox,
            "QDialog": QDialog,
            "QFileDialog": QFileDialog,
            "QFrame": QFrame,
            "QGridLayout": QGridLayout,
            "QHBoxLayout": QHBoxLayout,
            "QHeaderView": QHeaderView,
            "QIcon": QIcon,
            "QLabel": QLabel,
            "QLineEdit": QLineEdit,
            "QListWidget": QListWidget,
            "QListWidgetItem": QListWidgetItem,
            "QMessageBox": QMessageBox,
            "QObject": QObject,
            "pyqtSignal": pyqtSignal,
            "QPixmap": QPixmap,
            "QProgressBar": QProgressBar,
            "QPushButton": QPushButton,
            "QSpinBox": QSpinBox,
            "QSplitter": QSplitter,
            "Qt": Qt,
            "QTableWidget": QTableWidget,
            "QTableWidgetItem": QTableWidgetItem,
            "QTextBrowser": QTextBrowser,
            "QVBoxLayout": QVBoxLayout,
            "QWidget": QWidget,
        }
    except ImportError:
        return None


class BLCatalogConfigWidget:
    """Calibre Preferences -> Plugins configuration widget for BL-Catalog."""

    def __new__(cls, *args: Any, **kwargs: Any):
        qt = _import_qt()
        if qt is None:
            return super().__new__(cls)

        QWidget = qt["QWidget"]
        QVBoxLayout = qt["QVBoxLayout"]
        QGridLayout = qt["QGridLayout"]
        QLabel = qt["QLabel"]
        QLineEdit = qt["QLineEdit"]
        QComboBox = qt["QComboBox"]
        QSpinBox = qt["QSpinBox"]
        QCheckBox = qt["QCheckBox"]
        QFrame = qt["QFrame"]

        class _QtConfigWidget(QWidget):
            def __init__(self) -> None:
                super().__init__()
                self.setObjectName("MainSurface")
                self.setStyleSheet(build_stylesheet())
                prefs = get_prefs()

                root = QVBoxLayout(self)
                card = QFrame(self)
                card.setObjectName("Card")
                grid = QGridLayout(card)

                grid.addWidget(QLabel("BL-Catalog URL:"), 0, 0)
                self.url_edit = QLineEdit(str(prefs.get("url") or ""))
                grid.addWidget(self.url_edit, 0, 1)

                grid.addWidget(QLabel("Bearer Token (optional):"), 1, 0)
                self.token_edit = QLineEdit(str(prefs.get("token") or ""))
                self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
                grid.addWidget(self.token_edit, 1, 1)

                grid.addWidget(QLabel("Lookup Mode:"), 2, 0)
                self.mode_combo = QComboBox()
                self.mode_combo.addItem("Local SQLite Snapshot (fast full-library scan)", "snapshot")
                self.mode_combo.addItem("Live HTTP API (/v1/*)", "http")
                idx = 0 if prefs.get("mode") == "snapshot" else 1
                self.mode_combo.setCurrentIndex(idx)
                grid.addWidget(self.mode_combo, 2, 1)

                grid.addWidget(QLabel("Local Snapshot Path:"), 3, 0)
                self.snap_edit = QLineEdit(str(prefs.get("snapshot_path") or ""))
                grid.addWidget(self.snap_edit, 3, 1)

                grid.addWidget(QLabel("Max Books per Apply:"), 4, 0)
                self.max_spin = QSpinBox()
                self.max_spin.setRange(1, 5000)
                self.max_spin.setValue(int(prefs.get("max_apply") or 50))
                grid.addWidget(self.max_spin, 4, 1)

                grid.addWidget(QLabel("Audit Log Path (JSONL):"), 5, 0)
                self.log_edit = QLineEdit(str(prefs.get("log_path") or ""))
                grid.addWidget(self.log_edit, 5, 1)

                channel_label = QLabel("Plugin Update Channel:")
                channel_label.setObjectName("MutedLabel")
                grid.addWidget(channel_label, 6, 0)
                self.update_channel = QComboBox()
                self.update_channel.addItem("Stable", "stable")
                self.update_channel.addItem("Beta (includes prereleases)", "beta")
                self.update_channel.setCurrentIndex(1 if prefs.get("update_channel") == "beta" else 0)
                grid.addWidget(self.update_channel, 6, 1)

                self.chk_identifiers = QCheckBox("Identifiers (bl_catalog + missing valid ISBN)")
                self.chk_identifiers.setChecked(bool(prefs.get("apply_identifiers", True)))
                self.chk_title = QCheckBox("Title")
                self.chk_title.setChecked(bool(prefs.get("apply_title", False)))
                self.chk_authors = QCheckBox("Authors")
                self.chk_authors.setChecked(bool(prefs.get("apply_authors", False)))
                self.chk_series = QCheckBox("Series & Number")
                self.chk_series.setChecked(bool(prefs.get("apply_series", False)))
                self.chk_synopsis = QCheckBox("Synopsis (Comments)")
                self.chk_synopsis.setChecked(bool(prefs.get("apply_synopsis", False)))
                self.chk_tags = QCheckBox("Tags & Setting")
                self.chk_tags.setChecked(bool(prefs.get("apply_tags", False)))
                self.chk_cover = QCheckBox("Lexicanum Cover (verified SHA-256)")
                self.chk_cover.setChecked(bool(prefs.get("apply_cover", False)))

                for r_idx, chk in enumerate(
                    (
                        self.chk_identifiers,
                        self.chk_title,
                        self.chk_authors,
                        self.chk_series,
                        self.chk_synopsis,
                        self.chk_tags,
                        self.chk_cover,
                    ),
                    start=7,
                ):
                    grid.addWidget(chk, r_idx, 0, 1, 2)

                root.addWidget(card)
                root.addStretch(1)

            def save_settings(self) -> None:
                update_prefs({
                    "url": self.url_edit.text().strip(),
                    "token": self.token_edit.text().strip(),
                    "mode": self.mode_combo.currentData(),
                    "snapshot_path": self.snap_edit.text().strip(),
                    "max_apply": int(self.max_spin.value()),
                    "log_path": self.log_edit.text().strip(),
                    "apply_identifiers": self.chk_identifiers.isChecked(),
                    "apply_title": self.chk_title.isChecked(),
                    "apply_authors": self.chk_authors.isChecked(),
                    "apply_series": self.chk_series.isChecked(),
                    "apply_synopsis": self.chk_synopsis.isChecked(),
                    "apply_tags": self.chk_tags.isChecked(),
                    "apply_cover": self.chk_cover.isChecked(),
                    "update_channel": self.update_channel.currentData(),
                })

        return _QtConfigWidget()


def create_blc_dialog(gui: Any, icon: Any = None, selected_ids: list[int] | None = None):
    """Build and return the Reading Lamp Qt dialog inside Calibre."""
    qt = _import_qt()
    if qt is None:
        raise RuntimeError("Qt is not available outside Calibre")

    QDialog = qt["QDialog"]
    QFrame = qt["QFrame"]
    QVBoxLayout = qt["QVBoxLayout"]
    QHBoxLayout = qt["QHBoxLayout"]
    QLabel = qt["QLabel"]
    QLineEdit = qt["QLineEdit"]
    QPushButton = qt["QPushButton"]
    QComboBox = qt["QComboBox"]
    QCheckBox = qt["QCheckBox"]
    QSpinBox = qt["QSpinBox"]
    QProgressBar = qt["QProgressBar"]
    QSplitter = qt["QSplitter"]
    QTableWidget = qt["QTableWidget"]
    QTableWidgetItem = qt["QTableWidgetItem"]
    QHeaderView = qt["QHeaderView"]
    QAbstractItemView = qt["QAbstractItemView"]
    QTextBrowser = qt["QTextBrowser"]
    QListWidget = qt["QListWidget"]
    QListWidgetItem = qt["QListWidgetItem"]
    QFileDialog = qt["QFileDialog"]
    QMessageBox = qt["QMessageBox"]
    QObject = qt["QObject"]
    pyqtSignal = qt["pyqtSignal"]
    Qt = qt["Qt"]

    writer = CalibreDbWriter(gui.current_db.new_api)
    controller = DialogController(writer)

    class _JobBridge(QObject):
        """Carries worker-thread results back to the GUI thread through queued signals."""

        finished = pyqtSignal(object)
        progressed = pyqtSignal(int, int)

    class _Dialog(QDialog):
        def __init__(self) -> None:
            super().__init__(gui)
            self.setWindowTitle("bl-catalog \u00b7 Black Library Catalogue")
            if icon is not None:
                self.setWindowIcon(icon)
            self.resize(1240, 780)
            self.setStyleSheet(build_stylesheet())
            self._selected_ids = list(selected_ids or [])
            self._candidate_works: list[dict[str, Any]] = []
            self._bucket_buttons: dict[str, Any] = {}
            self._busy = False
            self._scan_completed = False
            self._cancel = threading.Event()
            self._job_done: Any = None
            self._job_title = ""
            self._locked: list[tuple[Any, bool]] = []
            self._bridge = _JobBridge(self)
            self._bridge.finished.connect(self._on_job_finished)
            self._bridge.progressed.connect(self._on_job_progress)

            root = QVBoxLayout(self)
            root.setContentsMargins(0, 0, 0, 0)
            root.setSpacing(0)

            # --- Sticky-style Header Bar (#2d353b) ---
            header = QFrame(self)
            header.setObjectName("HeaderBar")
            hbar = QHBoxLayout(header)
            hbar.setContentsMargins(20, 12, 20, 12)
            hbar.setSpacing(14)

            if icon is not None:
                icon_lbl = QLabel()
                icon_lbl.setPixmap(icon.pixmap(36, 36))
                hbar.addWidget(icon_lbl)

            brand = QLabel("BL-CATALOG")
            brand.setObjectName("BrandTitle")
            hbar.addWidget(brand)

            self.version_lbl = QLabel("snapshot \u00b7 ready")
            self.version_lbl.setObjectName("BrandVersion")
            hbar.addWidget(self.version_lbl)

            hbar.addStretch(1)

            self.mode_combo = QComboBox()
            self.mode_combo.addItem("Snapshot Mode", "snapshot")
            self.mode_combo.addItem("Live HTTP API", "http")
            self.mode_combo.setCurrentIndex(0 if controller.prefs.get("mode") == "snapshot" else 1)
            self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
            hbar.addWidget(self.mode_combo)

            self.sync_btn = QPushButton("Sync Snapshot")
            self.sync_btn.clicked.connect(self._on_sync_snapshot)
            hbar.addWidget(self.sync_btn)

            self.updates_btn = QPushButton("Plugin Updates")
            self.updates_btn.clicked.connect(self._on_plugin_updates)
            hbar.addWidget(self.updates_btn)

            self.filter_edit = QLineEdit()
            self.filter_edit.setPlaceholderText("Filter books, authors, ISBN, or work ID\u2026")
            self.filter_edit.setMinimumWidth(280)
            self.filter_edit.textChanged.connect(self._on_filter_changed)
            hbar.addWidget(self.filter_edit)

            root.addWidget(header)

            # --- Body container ---
            body = QVBoxLayout()
            body.setContentsMargins(20, 16, 20, 16)
            body.setSpacing(14)

            # --- Hero Card (#343f44) ---
            hero = QFrame(self)
            hero.setObjectName("HeroCard")
            hero_layout = QVBoxLayout(hero)
            hero_layout.setContentsMargins(18, 16, 18, 16)
            hero_layout.setSpacing(10)

            top_row = QHBoxLayout()
            state_col = QVBoxLayout()
            self.hero_state = QLabel("Ready to scan Calibre library")
            self.hero_state.setObjectName("HeroState")
            self.hero_sub = QLabel(
                f"Configured catalogue: {controller.prefs.get('url')} \u00b7 "
                f"{len(self._selected_ids)} selected in Calibre"
            )
            self.hero_sub.setObjectName("HeroSub")
            state_col.addWidget(self.hero_state)
            state_col.addWidget(self.hero_sub)
            top_row.addLayout(state_col, 1)

            ctl_row = QHBoxLayout()
            ctl_row.setSpacing(8)
            self.scan_sel_btn = QPushButton(f"Scan Selected ({len(self._selected_ids)})")
            self.scan_sel_btn.setObjectName("PrimaryButton")
            self.scan_sel_btn.setEnabled(bool(self._selected_ids))
            self.scan_sel_btn.clicked.connect(lambda: self._run_scan(selected_only=True))
            ctl_row.addWidget(self.scan_sel_btn)

            self.scan_all_btn = QPushButton("Scan Full Library")
            if not self._selected_ids:
                self.scan_all_btn.setObjectName("PrimaryButton")
            self.scan_all_btn.clicked.connect(lambda: self._run_scan(selected_only=False))
            ctl_row.addWidget(self.scan_all_btn)

            self.csv_btn = QPushButton("Export CSV")
            self.csv_btn.setEnabled(False)
            self.csv_btn.clicked.connect(self._on_export_csv)
            ctl_row.addWidget(self.csv_btn)

            self.cancel_btn = QPushButton("Cancel")
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.clicked.connect(self._on_cancel)
            ctl_row.addWidget(self.cancel_btn)

            top_row.addLayout(ctl_row)
            hero_layout.addLayout(top_row)

            self.progress = QProgressBar()
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            hero_layout.addWidget(self.progress)

            # Bucket filter pills
            pills_row = QHBoxLayout()
            pills_row.setSpacing(6)
            for b_key, b_title in (
                ("all", "All"),
                ("certain_isbn", "Certain \u00b7 ISBN"),
                ("certain_title_author", "Certain \u00b7 Title/Author"),
                ("picked", "Picked"),
                ("ambiguous", "Ambiguous"),
                ("conflict", "Conflict"),
                ("already", "Already"),
                ("not_in_blc", "Not in BLC"),
            ):
                btn = QPushButton(f"{b_title} (0)")
                btn.setCheckable(True)
                btn.setChecked(b_key == "all")
                btn.clicked.connect(lambda _checked, k=b_key: self._set_bucket(k))
                self._bucket_buttons[b_key] = (btn, b_title)
                pills_row.addWidget(btn)
            pills_row.addStretch(1)
            hero_layout.addLayout(pills_row)

            body.addWidget(hero)

            # --- Main Splitter: Left Table + Right Inspector/Picker ---
            splitter = QSplitter(Qt.Orientation.Horizontal, self)

            self.table = QTableWidget(0, 7)
            self.table.setHorizontalHeaderLabels([
                "ID",
                "Title",
                "Authors",
                "Bucket",
                "BLC Work",
                "Proposed Additions",
                "Reason",
            ])
            self.table.setAlternatingRowColors(True)
            self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.table.verticalHeader().setVisible(False)
            th = self.table.horizontalHeader()
            th.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            th.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            th.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            th.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
            th.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
            th.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
            self.table.itemSelectionChanged.connect(self._on_row_selected)
            table_panel = QFrame(self)
            table_layout = QVBoxLayout(table_panel)
            table_layout.setContentsMargins(0, 0, 0, 0)
            self.table_empty_lbl = QLabel()
            self.table_empty_lbl.setObjectName("MutedLabel")
            self.table_empty_lbl.setWordWrap(True)
            table_layout.addWidget(self.table_empty_lbl)
            table_layout.addWidget(self.table, 1)
            splitter.addWidget(table_panel)

            right_card = QFrame(self)
            right_card.setObjectName("Card")
            right_layout = QVBoxLayout(right_card)
            right_layout.setContentsMargins(14, 14, 14, 14)
            right_layout.setSpacing(8)

            insp_head = QLabel("WORK INSPECTOR & MANUAL PICKER")
            insp_head.setObjectName("SectionHeading")
            right_layout.addWidget(insp_head)

            self.detail_view = QTextBrowser()
            self.detail_view.setOpenExternalLinks(True)
            right_layout.addWidget(self.detail_view, 1)

            pick_head = QLabel("PICK CATALOGUE WORK (ANTHOLOGIES & OVERRIDES)")
            pick_head.setObjectName("SectionHeading")
            right_layout.addWidget(pick_head)

            pick_bar = QHBoxLayout()
            self.pick_search = QLineEdit()
            self.pick_search.setPlaceholderText("Search BLC title, author, ISBN, or w_id\u2026")
            self.pick_search.returnPressed.connect(self._on_search_blc)
            pick_bar.addWidget(self.pick_search, 1)
            self.pick_search_btn = QPushButton("Search BLC")
            self.pick_search_btn.clicked.connect(self._on_search_blc)
            pick_bar.addWidget(self.pick_search_btn)
            right_layout.addLayout(pick_bar)

            self.pick_status = QLabel("Search BL-Catalog to find a work to assign.")
            self.pick_status.setObjectName("MutedLabel")
            self.pick_status.setWordWrap(True)
            right_layout.addWidget(self.pick_status)

            self.pick_list = QListWidget()
            self.pick_list.setMaximumHeight(110)
            self.pick_list.currentRowChanged.connect(self._update_pick_enabled)
            right_layout.addWidget(self.pick_list)

            self.pick_apply_btn = QPushButton("Assign Picked Work to Selected Book")
            self.pick_apply_btn.setEnabled(False)
            self.pick_apply_btn.clicked.connect(self._on_assign_pick)
            right_layout.addWidget(self.pick_apply_btn)

            splitter.addWidget(right_card)
            splitter.setStretchFactor(0, 3)
            splitter.setStretchFactor(1, 2)
            body.addWidget(splitter, 1)

            # --- Footer Apply Card ---
            footer = QFrame(self)
            footer.setObjectName("Card")
            flayout = QHBoxLayout(footer)
            flayout.setContentsMargins(16, 12, 16, 12)
            flayout.setSpacing(12)

            self.chk_identifiers = QCheckBox("Identifiers (bl_catalog + missing ISBN)")
            self.chk_identifiers.setChecked(controller.options.identifiers)
            self.chk_title = QCheckBox("Title")
            self.chk_title.setChecked(controller.options.title)
            self.chk_authors = QCheckBox("Authors")
            self.chk_authors.setChecked(controller.options.authors)
            self.chk_series = QCheckBox("Series")
            self.chk_series.setChecked(controller.options.series)
            self.chk_synopsis = QCheckBox("Synopsis")
            self.chk_synopsis.setChecked(controller.options.synopsis)
            self.chk_tags = QCheckBox("Tags")
            self.chk_tags.setChecked(controller.options.tags)
            self.chk_cover = QCheckBox("Cover")
            self.chk_cover.setChecked(controller.options.cover)

            for chk in (
                self.chk_identifiers,
                self.chk_title,
                self.chk_authors,
                self.chk_series,
                self.chk_synopsis,
                self.chk_tags,
                self.chk_cover,
            ):
                chk.toggled.connect(self._on_options_toggled)
                flayout.addWidget(chk)

            flayout.addStretch(1)

            flayout.addWidget(QLabel("Max:"))
            self.max_spin = QSpinBox()
            self.max_spin.setRange(1, 5000)
            self.max_spin.setValue(int(controller.prefs.get("max_apply") or 50))
            flayout.addWidget(self.max_spin)

            self.apply_btn = QPushButton("Apply Visible Matches (0)")
            self.apply_btn.setObjectName("PrimaryButton")
            self.apply_btn.setEnabled(False)
            self.apply_btn.clicked.connect(self._on_apply)
            flayout.addWidget(self.apply_btn)

            body.addWidget(footer)
            root.addLayout(body, 1)
            self._populate_table()

        def _on_mode_changed(self) -> None:
            controller.prefs["mode"] = self.mode_combo.currentData()
            update_prefs({"mode": controller.prefs["mode"]})

        def _on_plugin_updates(self) -> None:
            from .update_ui import create_update_dialog

            create_update_dialog(self).exec()

        # Background jobs ---------------------------------------------------------------
        # Network and library-wide work runs on a worker thread so Calibre stays responsive.
        # Calibre's db.new_api is thread-safe; every widget update happens back on the GUI
        # thread in _on_job_finished / _on_job_progress.

        def _lockable_widgets(self) -> list[Any]:
            return [
                self.scan_sel_btn, self.scan_all_btn, self.sync_btn, self.csv_btn,
                self.apply_btn, self.pick_search_btn, self.pick_apply_btn, self.mode_combo, self.updates_btn,
                self.max_spin, self.chk_identifiers, self.chk_title, self.chk_authors,
                self.chk_series, self.chk_synopsis, self.chk_tags, self.chk_cover,
            ]

        def _start_job(self, title: str, fn: Any, on_done: Any, *, cancellable: bool) -> None:
            if self._busy:
                return
            self._busy = True
            self._cancel.clear()
            self._job_done = on_done
            self._job_title = title
            self._locked = [(w, w.isEnabled()) for w in self._lockable_widgets()]
            for w, _was in self._locked:
                w.setEnabled(False)
            self.cancel_btn.setEnabled(cancellable)

            def _run() -> None:
                try:
                    outcome = (True, fn())
                except BaseException as exc:  # noqa: BLE001 - delivered to the GUI thread
                    outcome = (False, exc)
                self._bridge.finished.emit(outcome)

            threading.Thread(target=_run, name="blc-calibre-job", daemon=True).start()

        def _on_job_progress(self, done: int, total: int) -> None:
            if total > 0:
                self.progress.setValue(int(done * 100 / total))

        def _on_job_finished(self, outcome: Any) -> None:
            ok, value = outcome
            on_done = self._job_done
            self._busy = False
            self._job_done = None
            for w, was in self._locked:
                w.setEnabled(was)
            self._locked = []
            self.cancel_btn.setEnabled(False)
            if not ok and self._job_title == "Scan":
                self._populate_table()
            if not ok and self._job_title == "BLC Search":
                self.pick_status.setText("Search failed. Check the catalogue connection and try again.")
            if ok:
                on_done(value)
            elif isinstance(value, OperationCancelled):
                self.hero_state.setText(f"{self._job_title} cancelled")
            elif isinstance(value, ApplyRefused):
                QMessageBox.warning(self, "Apply Refused", str(value))
            else:
                QMessageBox.warning(self, f"{self._job_title} Failed", str(value) or type(value).__name__)
            self._update_pick_enabled()

        def _on_cancel(self) -> None:
            if self._busy:
                self._cancel.set()
                self.cancel_btn.setEnabled(False)
                self.hero_sub.setText("Stopping after the current book\u2026")

        def reject(self) -> None:
            if self._busy:
                self._on_cancel()
                QMessageBox.information(
                    self,
                    "BL-Catalog Busy",
                    "Wait for the current operation to stop before closing.",
                )
                return
            super().reject()

        def closeEvent(self, event: Any) -> None:
            if self._busy:
                event.ignore()
                self.reject()
                return
            super().closeEvent(event)

        def _on_sync_snapshot(self) -> None:
            self.hero_state.setText("Downloading latest BLC snapshot\u2026")

            def _done(result: tuple[Path, str]) -> None:
                target, ver = result
                update_prefs({"mode": "snapshot", "snapshot_path": str(target)})
                self.mode_combo.setCurrentIndex(0)
                self.version_lbl.setText(f"snapshot \u00b7 {ver}")
                self.hero_state.setText(f"Snapshot {ver} synced")
                self.hero_sub.setText(f"Saved to {target}")

            self._start_job("Snapshot Sync", controller.sync_snapshot, _done, cancellable=False)

        def _on_options_toggled(self) -> None:
            opts = ApplyOptions(
                identifiers=self.chk_identifiers.isChecked(),
                title=self.chk_title.isChecked(),
                authors=self.chk_authors.isChecked(),
                series=self.chk_series.isChecked(),
                synopsis=self.chk_synopsis.isChecked(),
                tags=self.chk_tags.isChecked(),
                cover=self.chk_cover.isChecked(),
            )
            controller.set_options(opts)
            update_prefs(controller.prefs)
            self._populate_table(proposals_only=True)

        def _set_bucket(self, bucket: str) -> None:
            controller.bucket_filter = bucket
            for k, (btn, _title) in self._bucket_buttons.items():
                btn.setChecked(k == bucket)
            self._populate_table()

        def _on_filter_changed(self, text: str) -> None:
            controller.search_filter = text
            self._populate_table()

        def _run_scan(self, *, selected_only: bool) -> None:
            ids = self._selected_ids if selected_only else None
            self.hero_state.setText("Scanning books against BL-Catalog\u2026")
            self.progress.setValue(0)
            if not controller.rows:
                self.table_empty_lbl.setText("Scanning books. Matches will appear here.")
            cancel = self._cancel
            bridge = self._bridge

            def _prog(done: int, total: int, _row: dict[str, Any]) -> None:
                if cancel.is_set():
                    raise OperationCancelled()
                bridge.progressed.emit(done, total)

            self._start_job(
                "Scan",
                lambda: controller.scan(ids, progress=_prog),
                lambda _rows: self._after_scan(),
                cancellable=True,
            )

        def _after_scan(self) -> None:
            self._scan_completed = True
            self.progress.setValue(100)
            counts = controller.bucket_counts()
            for k, (btn, title) in self._bucket_buttons.items():
                btn.setText(f"{title} ({counts.get(k, 0)})")
            ready_count = sum(1 for r in controller.rows if is_applicable(r))
            if controller.catalog_version:
                self.version_lbl.setText(f"v{controller.catalog_version}")
            self.hero_state.setText(f"{ready_count} applicable matches ready \u00b7 {counts['all']} scanned")
            self.hero_sub.setText(
                f"certain_isbn: {counts['certain_isbn']} \u00b7 "
                f"certain_title_author: {counts['certain_title_author']} \u00b7 "
                f"ambiguous: {counts['ambiguous']} \u00b7 conflict: {counts['conflict']} \u00b7 "
                f"already: {counts['already']}"
            )
            self.csv_btn.setEnabled(bool(controller.rows))
            self.apply_btn.setEnabled(ready_count > 0)
            self._populate_table()

        def _populate_table(self, *, proposals_only: bool = False) -> None:
            updates_enabled = self.table.updatesEnabled()
            signals_blocked = self.table.blockSignals(True)
            self.table.setUpdatesEnabled(False)
            header = self.table.horizontalHeader()
            # ResizeToContents otherwise rescans existing rows for every replaced cell.
            resize_modes = [header.sectionResizeMode(i) for i in range(self.table.columnCount())]
            for i in range(self.table.columnCount()):
                header.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
            try:
                self._populate_table_items(proposals_only=proposals_only)
            finally:
                for i, mode in enumerate(resize_modes):
                    header.setSectionResizeMode(i, mode)
                self.table.blockSignals(signals_blocked)
                self.table.setUpdatesEnabled(updates_enabled)
            self._on_row_selected()

        def _proposal_text(self, row: dict[str, Any]) -> str:
            parts = [f"{k}:{v}" for k, v in sorted((row.get("add") or {}).items())]
            for field in ("title", "authors", "series", "synopsis", "tags", "cover"):
                if field in (row.get("proposals") or {}):
                    parts.append(f"+{field}")
            return ", ".join(parts) or "No changes"

        def _populate_table_items(self, *, proposals_only: bool = False) -> None:
            keep = self._selected_bids()
            shown = controller.filtered_rows()
            if proposals_only:
                for r_idx, row in enumerate(shown):
                    item = self.table.item(r_idx, 5)
                    text = self._proposal_text(row)
                    if item is not None and item.text() != text:
                        item.setText(text)
                return
            if controller.rows:
                empty_message = "No books match these filters. Choose All or change the filter text."
            elif self._scan_completed:
                empty_message = "No books were found in this scan. Add books in Calibre or scan the full library."
            else:
                empty_message = "No books scanned yet. Use Scan Selected or Scan Full Library to find matches."
            self.table_empty_lbl.setText(empty_message)
            self.table_empty_lbl.setVisible(not shown)
            self.table.setRowCount(len(shown))
            for r_idx, row in enumerate(shown):
                bid_item = QTableWidgetItem(str(row["id"]))
                bid_item.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
                self.table.setItem(r_idx, 0, bid_item)
                self.table.setItem(r_idx, 1, QTableWidgetItem(str(row.get("title") or "")))
                self.table.setItem(r_idx, 2, QTableWidgetItem(str(row.get("authors") or "")))

                b_style = BUCKET_STYLE.get(row["bucket"], BUCKET_STYLE["not_in_blc"])
                b_item = QTableWidgetItem(b_style["label"])
                self.table.setItem(r_idx, 3, b_item)

                wid_text = str(row.get("work_id") or "None")
                self.table.setItem(r_idx, 4, QTableWidgetItem(wid_text))

                self.table.setItem(r_idx, 5, QTableWidgetItem(self._proposal_text(row)))
                self.table.setItem(r_idx, 6, QTableWidgetItem(str(row.get("reason") or "")))

            # No automatic selection: an empty selection means "apply visible rows", so a
            # selected row must always be a deliberate choice. Rows the user already selected
            # stay selected while they remain visible.
            self.table.clearSelection()
            for r_idx, row in enumerate(shown):
                if int(row["id"]) in keep:
                    for c_idx in range(self.table.columnCount()):
                        cell = self.table.item(r_idx, c_idx)
                        if cell is not None:
                            cell.setSelected(True)

        def _selected_bids(self) -> set[int]:
            selected: set[int] = set()
            for item in self.table.selectedItems():
                id_item = self.table.item(item.row(), 0)
                if id_item is not None:
                    selected.add(int(id_item.data(Qt.ItemDataRole.UserRole)))
            return selected

        def _update_apply_label(self) -> None:
            selected = self._selected_bids()
            if selected:
                self.apply_btn.setText(f"Apply Selected ({len(selected)})")
            else:
                visible = sum(1 for r in controller.filtered_rows() if is_applicable(r))
                self.apply_btn.setText(f"Apply Visible Matches ({visible})")

        def _current_row(self) -> dict[str, Any] | None:
            items = self.table.selectedItems()
            if not items:
                return None
            r_idx = items[0].row()
            id_item = self.table.item(r_idx, 0)
            if id_item is None:
                return None
            bid = int(id_item.data(Qt.ItemDataRole.UserRole))
            for row in controller.rows:
                if int(row["id"]) == bid:
                    return row
            return None

        def _on_row_selected(self) -> None:
            self._update_apply_label()
            self._update_pick_enabled()
            row = self._current_row()
            if row is None:
                self.detail_view.setPlainText("Select a book to inspect its match and proposed updates.")
                return
            self.detail_view.setHtml(render_work_detail_html(row, row.get("work")))
            if not self.pick_search.text().strip():
                self.pick_search.setPlaceholderText(f"Search BLC for '{row.get('title')}'\u2026")

        def _update_pick_enabled(self) -> None:
            idx = self.pick_list.currentRow()
            self.pick_apply_btn.setEnabled(
                not self._busy and self._current_row() is not None
                and 0 <= idx < len(self._candidate_works)
            )

        def _on_search_blc(self) -> None:
            if self._busy:
                return
            row = self._current_row()
            q = self.pick_search.text().strip()
            if not q and row:
                q = str(row.get("title") or "")
            if not q:
                self.pick_status.setText("Enter a title, author, ISBN, or work ID to search BL-Catalog.")
                return
            self._candidate_works = []
            self.pick_list.clear()
            self.pick_status.setText("Searching BL-Catalog\u2026")
            self._update_pick_enabled()
            self._start_job(
                "BLC Search",
                lambda: controller.search_blc(q, limit=10),
                self._show_candidates,
                cancellable=False,
            )

        def _show_candidates(self, works: list[dict[str, Any]]) -> None:
            self._candidate_works = works
            self.pick_list.clear()
            self.pick_status.setText(
                f"{len(works)} work(s) found. Select a work and a Calibre book to assign it."
                if works else "No BL-Catalog works found. Try a different title, author, or ISBN."
            )
            for w in self._candidate_works:
                offered, kind = offered_credits(w)
                people = ", ".join(offered[:4] if offered else authors_of(w)[:4])
                if len(offered) > 4:
                    people += f" (+{len(offered) - 4} more)"
                label = f"{w['id']} \u00b7 {title_of(w)} \u00b7 {people or 'uncredited'} [{kind or 'work'}]"
                item = QListWidgetItem(label)
                self.pick_list.addItem(item)
            self._update_pick_enabled()

        def _on_assign_pick(self) -> None:
            row = self._current_row()
            idx = self.pick_list.currentRow()
            if row is None or idx < 0 or idx >= len(self._candidate_works):
                return
            work = self._candidate_works[idx]
            controller.pick_work_for_book(int(row["id"]), work)
            counts = controller.bucket_counts()
            for k, (btn, title) in self._bucket_buttons.items():
                btn.setText(f"{title} ({counts.get(k, 0)})")
            self.apply_btn.setEnabled(True)
            self._populate_table()
            self._on_row_selected()

        def _on_export_csv(self) -> None:
            path, _ = QFileDialog.getSaveFileName(
                self, "Export BLC Dry Run CSV", "/tmp/blc-dry-run.csv", "CSV Files (*.csv)"
            )
            if path:
                controller.export_csv(path)
                self.hero_sub.setText(f"Exported {len(controller.rows)} rows to {path}")

        def _on_apply(self) -> None:
            cap = int(self.max_spin.value())
            controller.prefs["max_apply"] = cap
            selected_bids = self._selected_bids()
            target_ids, eligible = controller.plan_apply(selected_bids, max_books=cap)
            if not target_ids:
                QMessageBox.information(
                    self, "Nothing to Apply", "No visible certain, picked, or updatable rows."
                )
                return
            if len(target_ids) > cap:
                QMessageBox.warning(
                    self,
                    "Apply Refused",
                    f"{len(target_ids)} books are selected, over the Max of {cap}.",
                )
                return
            scope = "selected" if selected_bids else "visible"
            note = f" (first {len(target_ids)} of {eligible} eligible)" if eligible > len(target_ids) else ""
            fields = ", ".join(
                name for name, chk in (
                    ("identifiers", self.chk_identifiers), ("title", self.chk_title),
                    ("authors", self.chk_authors), ("series", self.chk_series),
                    ("synopsis", self.chk_synopsis), ("tags", self.chk_tags),
                    ("cover", self.chk_cover),
                ) if chk.isChecked()
            ) or "nothing"
            answer = QMessageBox.question(
                self,
                "Apply BL-Catalog Matches",
                f"Write {fields} to {len(target_ids)} {scope} book(s){note}?\n\n"
                f"Book IDs: {', '.join(str(i) for i in target_ids[:20])}"
                + (" \u2026" if len(target_ids) > 20 else ""),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

            self.hero_state.setText(f"Applying to {len(target_ids)} book(s)\u2026")
            cancel = self._cancel
            self._start_job(
                "Apply",
                lambda: controller.apply(target_ids, max_books=cap, should_stop=cancel.is_set),
                self._after_apply,
                cancellable=True,
            )

        def _after_apply(self, results: list[dict[str, Any]]) -> None:
            counts = collections.Counter(r["status"] for r in results)
            self.hero_state.setText(
                f"Apply finished: {counts['written']} written, {counts['refused']} refused, "
                f"{counts['failed']} failed, {counts['cancelled']} cancelled"
            )
            self.hero_sub.setText(f"Audit log: {controller.prefs.get('log_path')} \u00b7 rescan to refresh rows")
            with contextlib.suppress(AttributeError, RuntimeError, TypeError):
                gui.library_view.model().refresh_ids([r["id"] for r in results if r["status"] == "written"])

    return _Dialog()

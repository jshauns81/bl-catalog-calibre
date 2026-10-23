"""Reading Lamp GUI dialog, state controller, and preferences widget for BL-Catalog in Calibre.

Separates pure-Python state and workflow logic (``DialogController``) from the Qt widget layer
(``BLCatalogDialog`` and ``BLCatalogConfigWidget``) so all scan, filter, manual work-pick,
CSV export, snapshot sync, and guarded apply flows can be tested headlessly.
"""

from __future__ import annotations

import collections
import contextlib
import copy
import hashlib
import json
import logging
import threading
import time
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
    CatalogueError,
    apply_manual_pick,
    authors_of,
    build_proposals,
    cover_of,
    cover_receipt_matches,
    demote_shared,
    offered_credits,
    run_scan,
    title_of,
    write_csv,
)
from .scan_cache import MATCHER_REVISION, ScanCache, default_cache_path, fingerprint
from .snapshot import migrate_snapshot_path
from .style import BUCKET_STYLE, build_stylesheet, render_work_detail_html
from .version import RELEASE_TAG


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
        scan_cache: ScanCache | None = None,
    ) -> None:
        self.writer = writer
        self.scan_cache = scan_cache
        self.scan_stats: dict[str, Any] = {}
        self.refresh_error = ""
        self.snapshot_notice = ""
        self.cache_needs_check = False
        self.saved_book_ids: list[int] = []
        self._raw_rows: dict[int, dict[str, Any]] = {}
        self.prefs = dict(prefs if prefs is not None else get_prefs())
        self.options: ApplyOptions = current_apply_options(self.prefs)
        self._injected_lookups = lookups
        self.books: list[dict[str, Any]] = []
        self.books_by_id: dict[int, dict[str, Any]] = {}
        self.rows: list[dict[str, Any]] = []
        self.bucket_filter: str = "all"
        self.search_filter: str = ""
        self.hide_stamped = bool(self.prefs.get("hide_stamped", False))
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
            self._refresh_proposals(row)

    def _refresh_proposals(self, row: dict[str, Any]) -> None:
        book = self.books_by_id.get(int(row["id"]))
        work = row.get("work")
        if book and work and row["bucket"] in APPLICABLE_BUCKETS + ("already",):
            with_id = row["bucket"] != "already" or str(row.get("work_id") or "") != str(row.get("bl_catalog") or "")
            proposals = build_proposals(book, work, self.options, picked=bool(row.get("picked")),
                                        with_id=with_id, cover_receipt=row.get("cover_receipt"))
            row["proposals"] = proposals
            row["add"] = dict(proposals.get("identifiers") or {})

    def validate_cover_receipt(self, book_id: int) -> bool:
        """Read one selected cover; invalidate its receipt when the stored bytes changed."""
        bid = int(book_id)
        row = next((row for row in self.rows if int(row["id"]) == bid), None)
        if row is None or not row.get("cover_receipt"):
            return False
        receipt = row["cover_receipt"]
        verified = False
        if cover_receipt_matches(receipt, cover_of(row.get("work") or {})):
            try:
                current = self.writer.cover_bytes(bid)
                verified = bool(current) and hashlib.sha256(current).hexdigest() == receipt["stored_sha256"]
            except Exception:  # noqa: BLE001 - receipt checks must not prevent inspecting a book
                verified = False
        if verified:
            return True
        row.pop("cover_receipt", None)
        self._refresh_proposals(row)
        raw = self._raw_rows.get(bid, row)
        raw.pop("cover_receipt", None)
        self._refresh_proposals(raw)
        version = row.get("matched_catalog_version") or self.catalog_version
        if self.scan_cache is not None and version:
            self.scan_cache.save(self.books_by_id, [raw], self._cache_scope(), version, set(self.books_by_id))
        return False

    def build_lookups(self) -> Lookups:
        if self._injected_lookups is not None:
            self.catalog_version = self._injected_lookups.version
            return self._injected_lookups
        http_client = HttpCatalogClient(
            self.prefs.get("url"),
            token=self.prefs.get("token"),
            timeout=float(self.prefs.get("timeout") or 10.0),
        )
        if self.prefs.get("mode") == "snapshot":
            snap_path = self._snapshot_path()
            if not Path(snap_path).is_file():
                raise CatalogueError("No local snapshot. Choose Sync Snapshot to download one, or select Live HTTP mode.")
            snap_client = SnapshotCatalogClient(snap_path, http_client=http_client)
            self.catalog_version = snap_client.version
            return Lookups(snap_client)
        return Lookups(http_client)

    def _snapshot_path(self) -> str:
        path = migrate_snapshot_path(self.prefs.get("snapshot_path"))
        if path != self.prefs.get("snapshot_path"):
            self.prefs["snapshot_path"] = path
            update_prefs({"snapshot_path": path})
        return path

    def sync_snapshot(self) -> tuple[Path, str]:
        """Download ``/v1/snapshots/latest.sqlite`` to the configured ``snapshot_path``."""
        http_client = HttpCatalogClient(
            self.prefs.get("url"),
            token=self.prefs.get("token"),
            timeout=float(self.prefs.get("timeout") or 10.0),
        )
        snap_path = self._snapshot_path()
        target, version = http_client.download_snapshot(snap_path)
        self.prefs["mode"] = "snapshot"
        self.prefs["snapshot_path"] = str(target)
        self.catalog_version = version
        self._injected_lookups = None
        return target, version

    def _cache_scope(self) -> str:
        return json.dumps([MATCHER_REVISION, self.prefs.get("url")], sort_keys=True)

    def _same_cache_scope(self, stored: str) -> bool:
        try:
            old = json.loads(stored)
            return isinstance(old, list) and old[:2] == json.loads(self._cache_scope())
        except (TypeError, ValueError):
            return False

    def restore_cache(self) -> bool:
        """Restore unchanged local books; Apply independently checks its selected targets."""
        if self.scan_cache is None:
            return False
        records = self.scan_cache.load()
        self.saved_book_ids = sorted(records)
        self.books = self.writer.read_books(None)
        self.books_by_id = {int(b["id"]): b for b in self.books}
        self._raw_rows = {
            bid: copy.deepcopy(entry["row"]) for bid, entry in records.items()
            if bid in self.books_by_id and self._same_cache_scope(entry["scope"])
            and entry["fingerprint"] == fingerprint(self.books_by_id[bid])
        }
        for bid, row in self._raw_rows.items():
            row["matched_catalog_version"] = records[bid]["version"]
        self.rows = list(copy.deepcopy(self._raw_rows).values())
        self.set_options(self.options)
        demote_shared(self.rows)
        self.cache_needs_check = False
        dates = [records[int(r["id"])]["scanned_at"] for r in self.rows]
        versions = {records[int(r["id"])]["version"] for r in self.rows}
        self.catalog_version = next(iter(versions)) if len(versions) == 1 else None
        self.snapshot_notice = ""
        if self._injected_lookups is not None:
            self.catalog_version = self._injected_lookups.version or self.catalog_version
        elif self.prefs.get("mode") == "snapshot":
            try:
                path = self._snapshot_path()
                if Path(path).is_file():
                    snapshot = SnapshotCatalogClient(path)
                    self.catalog_version = snapshot.version
                    snapshot.close()
                else:
                    self.snapshot_notice = "No local snapshot. Sync Snapshot once, or select Live HTTP before applying."
            except (CatalogueError, OSError) as exc:
                self.snapshot_notice = f"Local snapshot unavailable: {exc}. Use Sync Snapshot to replace it."
        self.scan_stats = {"saved_at": max(dates) if dates else ""}
        return bool(self.rows)

    def scan(
        self,
        book_ids: list[int] | tuple[int, ...] | None = None,
        progress=None,
        *,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        """Compare live books with saved fingerprints and rematch only invalidated rows."""
        all_books = self.writer.read_books(None)
        books_by_id = {int(b["id"]): b for b in all_books}
        wanted = {int(i) for i in book_ids} if book_ids is not None else set(books_by_id)
        target_books = [b for b in all_books if int(b["id"]) in wanted]
        lookups = self.build_lookups()
        backend = getattr(lookups, "backend", None)
        if isinstance(backend, HttpCatalogClient):
            version = backend.fetch_version()
        else:
            version = lookups.version
        scope = self._cache_scope()
        records = self.scan_cache.load() if self.scan_cache is not None else {}
        raw_rows = []
        reused = 0
        try:
            for n, book in enumerate(target_books, 1):
                bid = int(book["id"])
                entry = records.get(bid)
                if (not force and version and entry and self._same_cache_scope(entry["scope"])
                        and entry["version"] == version
                        and entry["fingerprint"] == fingerprint(book)):
                    row = copy.deepcopy(entry["row"])
                    reused += 1
                else:
                    row = run_scan([book], lookups, options=self.options, deduplicate=False)[0]
                    if (entry and self._same_cache_scope(entry["scope"]) and entry["row"].get("picked")
                            and entry["fingerprint"] == fingerprint(book) and row["bucket"] != "error"):
                        picked_work = lookups.work(entry["row"].get("work_id") or "")
                        if picked_work:
                            apply_manual_pick(row, book, picked_work, self.options)
                    if version and lookups.version and lookups.version != version:
                        raise CatalogueError("BLC changed during the scan. Run Scan Changes again.")
                if (entry and self._same_cache_scope(entry["scope"])
                        and entry["fingerprint"] == fingerprint(book) and entry["row"].get("cover_receipt")):
                    row["cover_receipt"] = copy.deepcopy(entry["row"]["cover_receipt"])
                elif self.scan_cache is None and self._raw_rows.get(bid, {}).get("cover_receipt"):
                    previous_book = self.books_by_id.get(bid)
                    if previous_book and fingerprint(previous_book) == fingerprint(book):
                        row["cover_receipt"] = copy.deepcopy(self._raw_rows[bid]["cover_receipt"])
                row["matched_catalog_version"] = version or lookups.version
                raw_rows.append(row)
                if progress:
                    progress(n, len(target_books), row)
            # Commit only after the complete operation, including cancellation checks.
            if self.scan_cache is not None and version:
                self.scan_cache.save(books_by_id, raw_rows, scope, version, set(books_by_id))
            self.books = all_books
            self.books_by_id = books_by_id
            self._raw_rows = {int(r["id"]): copy.deepcopy(r) for r in raw_rows}
            self.rows = raw_rows
            self.set_options(self.options)
            demote_shared(self.rows)
            self.catalog_version = version or lookups.version or self.catalog_version
            self.cache_needs_check = False
            self.scan_stats = {"reused": reused, "matched": len(raw_rows) - reused}
            return self.rows
        finally:
            if self._injected_lookups is None and isinstance(backend, SnapshotCatalogClient):
                backend.close()

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
            if self.hide_stamped and row.get("bl_catalog"):
                continue
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

    def clear_filters(self) -> None:
        self.bucket_filter = "all"
        self.search_filter = ""
        self.hide_stamped = False

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
                if self.cache_needs_check:
                    raise ApplyRefused("Run Scan Changes before assigning saved results")
                picked = apply_manual_pick(row, book, work, self.options)
                self._raw_rows[int(book_id)] = copy.deepcopy(picked)
                if self.scan_cache is not None and self.catalog_version:
                    self.scan_cache.save(self.books_by_id, [picked], self._cache_scope(),
                                         self.catalog_version, set(self.books_by_id))
                return picked
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
        progress: Any = None,
    ) -> list[dict[str, Any]]:
        """Run guarded apply on exactly ``book_ids`` (default: ``plan_apply()`` with no selection)."""
        if self.cache_needs_check:
            raise ApplyRefused("Run Scan Changes to check saved results before applying metadata")
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
                "cover_receipt": row.get("cover_receipt"),
            })

        lookups = self.build_lookups()
        try:
            results = apply_matches(
                targets,
                max_books=cap,
                books=all_books,
                lookups=lookups,
                writer=self.writer,
                log_path=log,
                options=self.options,
                should_stop=should_stop,
                progress=progress,
            )
            self.refresh_error = ""
            if progress is not None:
                try:
                    progress(len(results), len(wanted), {
                        "phase": "refreshing", "id": None, "title": "", "current": len(wanted),
                    })
                except Exception:
                    logging.getLogger(__name__).exception("Could not report Calibre Apply refresh progress")
            try:
                self._refresh_written_rows(results, lookups)
            except Exception as exc:  # noqa: BLE001 - writes already succeeded; report refresh separately
                self.refresh_error = str(exc) or type(exc).__name__
            return results
        finally:
            backend = getattr(lookups, "backend", None)
            if self._injected_lookups is None and isinstance(backend, SnapshotCatalogClient):
                backend.close()

    def _refresh_written_rows(self, results: list[dict[str, Any]], lookups: Any) -> None:
        written = [result for result in results if result["status"] == "written"]
        if not written:
            return
        self.catalog_version = lookups.version or self.catalog_version
        fresh_books = self.writer.read_books(None)
        fresh_by_id = {int(book["id"]): book for book in fresh_books}
        raw = {int(row["id"]): copy.deepcopy(self._raw_rows.get(int(row["id"]), row))
               for row in self.rows if int(row["id"]) in fresh_by_id}
        refreshed = []
        for result in written:
            bid = int(result["id"])
            if bid not in fresh_by_id:
                continue
            book = fresh_by_id[bid]
            row = run_scan([book], lookups, options=self.options, deduplicate=False)[0]
            old = raw.get(bid, {})
            if old.get("picked") and not (book.get("identifiers") or {}).get("bl_catalog"):
                work = lookups.work(result["work_id"])
                if work:
                    apply_manual_pick(row, book, work, self.options)
            if row["bucket"] == "error":
                raise CatalogueError(row.get("reason") or "Could not refresh a written book")
            self.catalog_version = lookups.version or self.catalog_version
            row["applied_at"] = result.get("at", "")
            row["matched_catalog_version"] = self.catalog_version
            row["updated_fields"] = list(result.get("fields_updated") or [])
            if "cover" in row["updated_fields"]:
                try:
                    stored_cover = self.writer.cover_bytes(bid)
                    catalogue_sha = result.get("catalogue_cover_sha256") or (cover_of(row.get("work") or {}) or {}).get("sha256")
                    if stored_cover and catalogue_sha:
                        row["cover_receipt"] = {
                            "catalogue_sha256": str(catalogue_sha).lower(),
                            "stored_sha256": hashlib.sha256(stored_cover).hexdigest(),
                        }
                except Exception as exc:  # noqa: BLE001 - receipt failure must not hide a successful write
                    row["cover_receipt_error"] = str(exc) or type(exc).__name__
            elif old.get("cover_receipt"):
                row["cover_receipt"] = copy.deepcopy(old["cover_receipt"])
            if result.get("added"):
                row["updated_fields"].insert(0, "identifiers")
            raw[bid] = row
            refreshed.append(row)
        self.books = fresh_books
        self.books_by_id = fresh_by_id
        self._raw_rows = raw
        self.rows = copy.deepcopy(list(raw.values()))
        self.set_options(self.options)
        demote_shared(self.rows)
        if self.scan_cache is not None and self.catalog_version:
            self.scan_cache.save(fresh_by_id, refreshed, self._cache_scope(),
                                 self.catalog_version, set(fresh_by_id))


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
            QLayout,
            QLineEdit,
            QListWidget,
            QListWidgetItem,
            QMessageBox,
            QObject,
            QPixmap,
            QProgressBar,
            QPushButton,
            QSpinBox,
            Qt,
            QTableWidget,
            QTableWidgetItem,
            QTabWidget,
            QTextBrowser,
            QTimer,
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
            "QLayout": QLayout,
            "QListWidget": QListWidget,
            "QListWidgetItem": QListWidgetItem,
            "QMessageBox": QMessageBox,
            "QObject": QObject,
            "pyqtSignal": pyqtSignal,
            "QPixmap": QPixmap,
            "QProgressBar": QProgressBar,
            "QPushButton": QPushButton,
            "QSpinBox": QSpinBox,
            "Qt": Qt,
            "QTableWidget": QTableWidget,
            "QTableWidgetItem": QTableWidgetItem,
            "QTabWidget": QTabWidget,
            "QTimer": QTimer,
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
    QTableWidget = qt["QTableWidget"]
    QTableWidgetItem = qt["QTableWidgetItem"]
    QTabWidget = qt["QTabWidget"]
    QGridLayout = qt["QGridLayout"]
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

    library_db = gui.current_db
    writer = CalibreDbWriter(library_db.new_api)
    cache_path = default_cache_path()
    library_path = getattr(gui.current_db, "library_path", None)
    cache = ScanCache(cache_path, str(Path(library_path).resolve())) if cache_path and library_path else None
    controller = DialogController(writer, scan_cache=cache)

    class _JobBridge(QObject):
        """Carries worker-thread results back to the GUI thread through queued signals."""

        finished = pyqtSignal(object)
        progressed = pyqtSignal(int, int)
        apply_progressed = pyqtSignal(int, int, object)

    class _Dialog(QDialog):
        def __init__(self) -> None:
            # A parented dialog stays above Calibre on several window managers.
            super().__init__(None, Qt.WindowType.Window | Qt.WindowType.WindowMinMaxButtonsHint
                             | Qt.WindowType.WindowCloseButtonHint)
            self.setWindowModality(Qt.WindowModality.NonModal)
            self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
            self.setWindowTitle(f"bl-catalog {RELEASE_TAG} \u00b7 Black Library Catalogue")
            if icon is not None:
                self.setWindowIcon(icon)
            self.resize(1240, 780)
            self.setStyleSheet(build_stylesheet())
            self._selected_ids = list(selected_ids or [])
            self._candidate_works: list[dict[str, Any]] = []
            self._bucket_buttons: dict[str, Any] = {}
            self._busy = False
            self._retired = False
            self._library_selection = None
            self._scan_completed = False
            self._cancel = threading.Event()
            self._job_done: Any = None
            self._job_title = ""
            self._locked: list[tuple[Any, bool]] = []
            self._bridge = _JobBridge(self)
            self._bridge.finished.connect(self._on_job_finished)
            self._bridge.progressed.connect(self._on_job_progress)
            self._bridge.apply_progressed.connect(self._on_job_apply_progress)
            self._apply_event: dict[str, Any] | None = None
            self._elapsed_timer = qt["QTimer"](self)
            self._elapsed_timer.setInterval(1000)
            self._elapsed_timer.timeout.connect(self._update_elapsed)

            root = QVBoxLayout(self)
            root.setSizeConstraint(qt["QLayout"].SizeConstraint.SetNoConstraint)
            self.setMinimumSize(1000, 720)
            root.setContentsMargins(0, 0, 0, 0)
            root.setSpacing(0)

            # --- Sticky-style Header Bar (#2d353b) ---
            header = QFrame(self)
            header.setObjectName("HeaderBar")
            hbar = QHBoxLayout(header)
            hbar.setContentsMargins(20, 12, 20, 12)
            hbar.setSpacing(12)

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
            self.filter_edit.setClearButtonEnabled(True)
            self.filter_edit.textChanged.connect(self._on_filter_changed)

            root.addWidget(header)

            # --- Body container ---
            body = QVBoxLayout()
            body.setContentsMargins(20, 12, 20, 16)
            body.setSpacing(12)

            # --- Hero Card (#343f44) ---
            hero = QFrame(self)
            hero.setObjectName("HeroCard")
            hero_layout = QVBoxLayout(hero)
            hero_layout.setContentsMargins(16, 12, 16, 12)
            hero_layout.setSpacing(8)

            state_col = QVBoxLayout()
            self.hero_state = QLabel("Ready to scan Calibre library")
            self.hero_state.setObjectName("HeroState")
            self.hero_state.setWordWrap(True)
            self.hero_sub = QLabel(
                f"Configured catalogue: {controller.prefs.get('url')} \u00b7 "
                f"{len(self._selected_ids)} selected in Calibre"
            )
            self.hero_sub.setObjectName("HeroSub")
            self.hero_sub.setWordWrap(True)
            state_col.addWidget(self.hero_state)
            state_col.addWidget(self.hero_sub)
            hero_layout.addLayout(state_col)

            ctl_row = QHBoxLayout()
            ctl_row.setSpacing(8)
            self.scan_sel_btn = QPushButton(f"Scan Selected ({len(self._selected_ids)})")
            self.scan_sel_btn.setEnabled(bool(self._selected_ids))
            self.scan_sel_btn.clicked.connect(lambda: self._run_scan(selected_only=True))
            ctl_row.addWidget(self.scan_sel_btn)

            self.scan_all_btn = QPushButton("Scan Changes")
            self.scan_all_btn.clicked.connect(lambda: self._run_scan(selected_only=False))
            ctl_row.addWidget(self.scan_all_btn)
            self.rescan_btn = QPushButton("Force Full Rescan")
            self.rescan_btn.clicked.connect(lambda: self._run_scan(selected_only=False, force=True))
            ctl_row.addWidget(self.rescan_btn)
            ctl_row.addStretch(1)
            self.elapsed_lbl = QLabel("")
            self.elapsed_lbl.setObjectName("MutedLabel")
            ctl_row.addWidget(self.elapsed_lbl)

            self.csv_btn = QPushButton("Export CSV")
            self.csv_btn.setEnabled(False)
            self.csv_btn.clicked.connect(self._on_export_csv)
            ctl_row.addWidget(self.csv_btn)

            self.cancel_btn = QPushButton("Cancel")
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.clicked.connect(self._on_cancel)
            ctl_row.addWidget(self.cancel_btn)

            hero_layout.addLayout(ctl_row)

            self.operation_progress_lbl = QLabel("")
            self.operation_progress_lbl.setObjectName("OperationProgress")
            self.operation_progress_lbl.setTextFormat(Qt.TextFormat.PlainText)
            self.operation_progress_lbl.setWordWrap(True)
            self.operation_progress_lbl.setVisible(False)
            hero_layout.addWidget(self.operation_progress_lbl)

            self.progress = QProgressBar()
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.progress.setTextVisible(False)
            self.progress.setVisible(False)
            hero_layout.addWidget(self.progress)

            # Bucket filter pills
            self._bucket_bar = QFrame()
            self._bucket_grid = QGridLayout(self._bucket_bar)
            self._bucket_grid.setContentsMargins(0, 0, 0, 0)
            self._bucket_grid.setSpacing(6)
            self._bucket_columns = 4
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
                btn.setObjectName("BucketButton")
                btn.setCheckable(True)
                btn.setChecked(b_key == "all")
                btn.clicked.connect(lambda _checked, k=b_key: self._set_bucket(k))
                self._bucket_buttons[b_key] = (btn, b_title)
                index = len(self._bucket_buttons) - 1
                self._bucket_grid.addWidget(btn, index // 4, index % 4)
            hero_layout.addWidget(self._bucket_bar)

            body.addWidget(hero)

            filter_row = QHBoxLayout()
            filter_row.setSpacing(12)
            filter_row.addWidget(self.filter_edit, 1)
            self.clear_filters_btn = QPushButton("Clear Filters")
            self.clear_filters_btn.clicked.connect(self._clear_filters)
            filter_row.addWidget(self.clear_filters_btn)
            self.hide_stamped_chk = QCheckBox("Hide BLC-stamped books")
            self.hide_stamped_chk.setChecked(controller.hide_stamped)
            self.hide_stamped_chk.toggled.connect(self._on_hide_stamped)
            filter_row.addWidget(self.hide_stamped_chk)
            body.addLayout(filter_row)

            # --- Results table and fixed-width inspector ---
            self.results_panel = QFrame(self)
            self.results_panel.setObjectName("ResultsPanel")
            results_layout = QHBoxLayout(self.results_panel)
            results_layout.setContentsMargins(8, 8, 8, 8)
            results_layout.setSpacing(12)

            self.table = QTableWidget(0, 7)
            self.table.setObjectName("MatchResults")
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
            # Stable widths avoid scanning thousands of cells during window resizing.
            th.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            for column, width in enumerate((52, 240, 180, 150, 140, 175, 220)):
                self.table.setColumnWidth(column, width)
            th.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            self.table.setWordWrap(False)
            self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
            self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
            self.table.verticalHeader().setDefaultSectionSize(max(30, self.table.fontMetrics().height() + 12))
            self.table.setColumnHidden(4, True)
            self.table.setColumnHidden(6, True)
            self.table.itemSelectionChanged.connect(self._on_row_selected)
            self.table.currentCellChanged.connect(self._on_current_book_changed)
            self.table.cellClicked.connect(self._on_current_book_changed)
            table_panel = QFrame(self)
            table_layout = QVBoxLayout(table_panel)
            table_layout.setContentsMargins(0, 0, 0, 0)
            self.table_empty_lbl = QLabel()
            self.table_empty_lbl.setObjectName("MutedLabel")
            self.table_empty_lbl.setWordWrap(True)
            self.details_columns = QCheckBox("Show work ID and reason columns")
            self.details_columns.toggled.connect(self._on_detail_columns)
            table_layout.setSpacing(6)
            filter_row.addWidget(self.details_columns)
            navigation_row = QHBoxLayout()
            navigation_row.addWidget(self.table_empty_lbl, 1)
            self.calibre_link_status = QLabel()
            self.calibre_link_status.setObjectName("MutedLabel")
            self.calibre_link_status.setWordWrap(True)
            self.calibre_link_status.setVisible(False)
            navigation_row.addWidget(self.calibre_link_status, 1)
            navigation_row.addStretch(1)
            self.show_calibre_btn = QPushButton("Show in Calibre")
            self.show_calibre_btn.setAutoDefault(False)
            self.show_calibre_btn.setEnabled(False)
            self.show_calibre_btn.clicked.connect(lambda: self._show_in_calibre(activate=True))
            navigation_row.addWidget(self.show_calibre_btn)
            table_layout.addLayout(navigation_row)
            table_layout.addWidget(self.table, 1)
            results_layout.addWidget(table_panel, 1)

            right_card = QFrame(self)
            right_card.setObjectName("InspectorPane")
            right_card.setFixedWidth(320)
            right_layout = QVBoxLayout(right_card)
            right_layout.setContentsMargins(8, 0, 0, 0)
            right_layout.setSpacing(8)
            self.inspector_tabs = QTabWidget()
            right_layout.addWidget(self.inspector_tabs)
            self.detail_view = QTextBrowser()
            self.detail_view.setOpenExternalLinks(True)
            self.inspector_tabs.addTab(self.detail_view, "Book details")
            picker = qt["QWidget"]()
            picker_layout = QVBoxLayout(picker)
            picker_layout.setContentsMargins(4, 12, 4, 0)
            picker_layout.setSpacing(8)
            self.inspector_tabs.addTab(picker, "Manual match")

            pick_head = QLabel("PICK CATALOGUE WORK (ANTHOLOGIES & OVERRIDES)")
            pick_head.setObjectName("SectionHeading")
            pick_head.setWordWrap(True)
            picker_layout.addWidget(pick_head)

            pick_bar = QHBoxLayout()
            self.pick_search = QLineEdit()
            self.pick_search.setPlaceholderText("Search BLC title, author, ISBN, or w_id\u2026")
            self.pick_search.returnPressed.connect(self._on_search_blc)
            pick_bar.addWidget(self.pick_search, 1)
            self.pick_search_btn = QPushButton("Search BLC")
            self.pick_search_btn.clicked.connect(self._on_search_blc)
            pick_bar.addWidget(self.pick_search_btn)
            picker_layout.addLayout(pick_bar)

            self.pick_status = QLabel("Search BL-Catalog to find a work to assign.")
            self.pick_status.setObjectName("MutedLabel")
            self.pick_status.setWordWrap(True)
            picker_layout.addWidget(self.pick_status)

            self.pick_list = QListWidget()
            self.pick_list.currentRowChanged.connect(self._update_pick_enabled)
            picker_layout.addWidget(self.pick_list, 1)

            self.pick_apply_btn = QPushButton("Assign Work to Selected Book")
            self.pick_apply_btn.setEnabled(False)
            self.pick_apply_btn.clicked.connect(self._on_assign_pick)
            picker_layout.addWidget(self.pick_apply_btn)

            results_layout.addWidget(right_card)
            body.addWidget(self.results_panel, 1)

            # --- Footer Apply Card ---
            footer = QFrame(self)
            footer.setObjectName("Card")
            footer_layout = QGridLayout(footer)
            footer_layout.setContentsMargins(16, 12, 16, 12)
            footer_layout.setSpacing(8)
            self._field_bar = QFrame()
            self._field_grid = QGridLayout(self._field_bar)
            self._field_grid.setContentsMargins(0, 0, 0, 0)
            self._field_grid.setHorizontalSpacing(16)
            self._field_grid.setVerticalSpacing(8)
            self._field_columns = 4
            footer_layout.addWidget(self._field_bar, 0, 0)
            self._apply_bar = QFrame()
            flayout = QHBoxLayout(self._apply_bar)
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

            self._field_checks = (
                self.chk_identifiers,
                self.chk_title,
                self.chk_authors,
                self.chk_series,
                self.chk_synopsis,
                self.chk_tags,
                self.chk_cover,
            )
            for index, chk in enumerate(self._field_checks):
                chk.toggled.connect(self._on_options_toggled)
                self._field_grid.addWidget(chk, index // 4, index % 4)

            flayout.addStretch(1)

            flayout.addWidget(QLabel("Max:"))
            self.max_spin = QSpinBox()
            self.max_spin.setRange(1, 5000)
            self.max_spin.setValue(int(controller.prefs.get("max_apply") or 50))
            self.max_spin.editingFinished.connect(self._on_max_changed)
            flayout.addWidget(self.max_spin)

            self.apply_btn = QPushButton("Apply Visible Matches (0)")
            self.apply_btn.setObjectName("PrimaryButton")
            self.apply_btn.setEnabled(False)
            self.apply_btn.clicked.connect(self._on_apply)
            flayout.addWidget(self.apply_btn)
            flayout.setContentsMargins(0, 0, 0, 0)
            footer_layout.addWidget(self._apply_bar, 1, 0)
            self._footer_grid = footer_layout
            self._footer_inline = False

            body.addWidget(footer)
            root.addLayout(body, 1)
            self._populate_table()
            self._reflow_controls()
            try:
                self._library_selection = gui.library_view.selectionModel()
                self._library_selection.selectionChanged.connect(self.refresh_calibre_selection)
            except AttributeError:
                pass
            self.finished.connect(self._disconnect_calibre_selection)
            if controller.scan_cache is not None:
                self._start_job("Load saved scan", controller.restore_cache, self._after_restore, cancellable=False)

        def resizeEvent(self, event: Any) -> None:
            super().resizeEvent(event)
            if hasattr(self, "_field_checks"):
                self._reflow_controls()

        def _reflow_controls(self) -> None:
            for grid, bar, widgets, attr in (
                (self._bucket_grid, self._bucket_bar,
                 [button for button, _label in self._bucket_buttons.values()], "_bucket_columns"),
                (self._field_grid, self._field_bar, self._field_checks, "_field_columns"),
            ):
                gap = grid.horizontalSpacing()
                required = sum(widget.sizeHint().width() for widget in widgets) + gap * (len(widgets) - 1)
                available = max(0, self.width() - 76)
                columns = len(widgets) if required <= available else 4
                if columns == getattr(self, attr):
                    continue
                for widget in widgets:
                    grid.removeWidget(widget)
                for column in range(8):
                    grid.setColumnStretch(column, 0)
                for index, widget in enumerate(widgets):
                    grid.addWidget(widget, index // columns, index % columns)
                setattr(self, attr, columns)
            field_width = sum(widget.sizeHint().width() for widget in self._field_checks) + 16 * 6
            inline = field_width + self._apply_bar.sizeHint().width() + 12 <= self.width() - 76
            if inline != self._footer_inline:
                self._footer_grid.removeWidget(self._apply_bar)
                self._footer_grid.addWidget(self._apply_bar, 0 if inline else 1, 1 if inline else 0)
                self._footer_inline = inline

        def _after_restore(self, restored: bool) -> None:
            if restored:
                self._after_scan()
                self.hero_state.setText(f"{len(controller.rows)} saved matches ready")
                notice = controller.snapshot_notice or (
                    "Saved matches kept. Apply rechecks only selected books. Snapshot updates are manual."
                )
                self.hero_sub.setText(notice)
            elif controller.scan_cache and controller.scan_cache.error:
                self.hero_sub.setText("Saved scan unavailable; scan again. " + controller.scan_cache.error)
            elif controller.saved_book_ids:
                self.hero_state.setText("Saved books changed; scan the selected books to refresh them")

        def _on_mode_changed(self) -> None:
            controller.prefs["mode"] = self.mode_combo.currentData()
            update_prefs({"mode": controller.prefs["mode"]})
            if controller.rows:
                self.hero_sub.setText("Source changed. Saved matches are kept; Apply rechecks selected books.")
                self._update_apply_label()

        def _on_plugin_updates(self) -> None:
            from .update_ui import create_update_dialog

            create_update_dialog(self).exec()

        # Background jobs ---------------------------------------------------------------
        # Network and library-wide work runs on a worker thread so Calibre stays responsive.
        # Calibre's db.new_api is thread-safe; every widget update happens back on the GUI
        # thread in _on_job_finished / _on_job_progress.

        def _lockable_widgets(self) -> list[Any]:
            return [
                self.scan_sel_btn, self.scan_all_btn, self.rescan_btn, self.sync_btn, self.csv_btn,
                self.apply_btn, self.pick_search_btn, self.pick_apply_btn, self.mode_combo, self.updates_btn,
                self.max_spin, self.chk_identifiers, self.chk_title, self.chk_authors,
                self.chk_series, self.chk_synopsis, self.chk_tags, self.chk_cover,
            ]

        def _start_job(self, title: str, fn: Any, on_done: Any, *, cancellable: bool) -> None:
            if self._busy or self._retired or gui.current_db is not library_db:
                return
            self._busy = True
            self._cancel.clear()
            self._job_done = on_done
            self._job_title = title
            self._job_started = time.monotonic()
            self._apply_event = None
            self._update_elapsed()
            self._elapsed_timer.start()
            initial_stage = {
                "Scan": "Reading Calibre metadata and checking saved matches…",
                "Apply": "Preparing selected books and reading current Calibre metadata…",
                "Load saved scan": "Loading saved matches and checking local Calibre metadata…",
                "Snapshot Sync": "Downloading and validating the catalogue snapshot…",
                "BLC Search": "Searching the catalogue…",
            }.get(title, f"{title}…")
            self.operation_progress_lbl.setText(initial_stage)
            self.operation_progress_lbl.setVisible(True)
            self.progress.setRange(0, 0)
            self.progress.setVisible(True)
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

        def _update_elapsed(self) -> None:
            elapsed = time.monotonic() - self._job_started
            self.elapsed_lbl.setText(f"{self._job_title} · {elapsed:.0f}s")

        def _on_job_progress(self, done: int, total: int) -> None:
            if self._busy and total > 0:
                done = max(0, min(done, total))
                percentage = round(done * 100 / total)
                self.progress.setRange(0, 100)
                self.progress.setValue(percentage)
                text = f"{done} of {total} books ({percentage}%)"
                if self._cancel.is_set():
                    text += " · Stopping after the current book…"
                self.operation_progress_lbl.setText(text)

        def _on_job_apply_progress(self, done: int, total: int, event: Any) -> None:
            if not self._busy or self._job_title != "Apply":
                return
            event = dict(event) if isinstance(event, dict) else {}
            self._apply_event = event
            phase = event.get("phase")
            done = max(0, min(done, total)) if total > 0 else 0
            percentage = round(done * 100 / total) if total > 0 else 0
            if total > 0:
                self.progress.setRange(0, 100)
                self.progress.setValue(percentage)
            if phase == "refreshing":
                text = f"Saving refreshed results · {done} of {total} processed ({percentage}%)"
            else:
                stage = {
                    "checking": f"Checking match {event.get('current', 1)} of {total}",
                    "cover": "Downloading cover",
                    "writing": "Writing metadata",
                    "verifying": "Verifying saved changes",
                    "written": "Book updated",
                    "refused": "Book refused",
                    "failed": "Book failed",
                    "cancelled": "Book cancelled",
                }.get(phase, "Processing book")
                text = f"{done} of {total} complete ({percentage}%) · {stage}"
                if event.get("id") is not None:
                    text += f" · Book {event['id']}"
                    if event.get("title"):
                        text += f": {event['title']}"
            if self._cancel.is_set():
                text += " · Stopping after the current step…"
            self.operation_progress_lbl.setText(text)

        def _on_job_finished(self, outcome: Any) -> None:
            ok, value = outcome
            on_done = self._job_done
            self._busy = False
            self._job_done = None
            self._elapsed_timer.stop()
            elapsed = time.monotonic() - self._job_started
            self.elapsed_lbl.setText(f"{self._job_title} · {elapsed:.1f}s")
            self.progress.setVisible(False)
            self.progress.setRange(0, 100)
            self.operation_progress_lbl.setVisible(False)
            self.operation_progress_lbl.clear()
            for w, was in self._locked:
                w.setEnabled(was)
            self._locked = []
            self.cancel_btn.setEnabled(False)
            if self._retired:
                super().reject()
                return
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
            self.refresh_calibre_selection()

        def _disconnect_calibre_selection(self, *_args) -> None:
            if self._library_selection is not None:
                with contextlib.suppress(TypeError, RuntimeError):
                    self._library_selection.selectionChanged.disconnect(self.refresh_calibre_selection)
                self._library_selection = None

        def retire_library(self) -> None:
            self._retired = True
            self._cancel.set()
            self._disconnect_calibre_selection()
            # Wait only for local write/read-back/rollback, never a network request.
            if hasattr(writer, "detach"):
                writer.detach()
            self.hide()
            if not self._busy:
                super().reject()

        def refresh_calibre_selection(self, *_args) -> None:
            if self._retired or gui.current_db is not library_db:
                return
            try:
                view = gui.library_view
                self._selected_ids = [int(view.model().id(idx)) for idx in view.selectionModel().selectedRows()]
            except (AttributeError, TypeError, ValueError):
                return
            self.scan_sel_btn.setText(f"Scan Selected ({len(self._selected_ids)})")
            if not self._busy:
                self.scan_sel_btn.setEnabled(bool(self._selected_ids))

        def _on_cancel(self) -> None:
            if self._busy:
                self._cancel.set()
                self.cancel_btn.setEnabled(False)
                event = self._apply_event or {}
                if event.get("phase") == "refreshing":
                    text = "Finishing saved-result refresh before stopping…"
                elif event.get("id") is not None:
                    text = f"Stopping after Book {event['id']}"
                    if event.get("title"):
                        text += f": {event['title']}"
                    text += " finishes…"
                else:
                    text = "Stopping after the current step…"
                self.hero_sub.setText(text)
                self.operation_progress_lbl.setText(text)

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
            answer = QMessageBox.question(
                self, "Update Local BLC Snapshot",
                "Download and activate the latest catalogue snapshot?\n\n"
                "Your saved matches will be kept. This will not start a library scan.\n"
                "Apply rechecks the books you choose against the active snapshot.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.hero_state.setText("Downloading latest BLC snapshot…")

            def _done(result: tuple[Path, str]) -> None:
                target, ver = result
                update_prefs({"mode": "snapshot", "snapshot_path": str(target)})
                self.mode_combo.setCurrentIndex(0)
                controller.cache_needs_check = False
                self._after_scan()
                self.hero_state.setText(f"Snapshot {ver} active; saved matches kept")
                self.hero_sub.setText(f"Saved to {target}. Apply checks selected books; Scan Changes refreshes previews.")

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
            update_prefs({key: value for key, value in controller.prefs.items() if key.startswith("apply_")})
            self._populate_table(proposals_only=True)

        def _set_bucket(self, bucket: str) -> None:
            controller.bucket_filter = bucket
            for k, (btn, _title) in self._bucket_buttons.items():
                btn.setChecked(k == bucket)
            self._populate_table()
            self._reflow_controls()

        def _on_filter_changed(self, text: str) -> None:
            controller.search_filter = text
            self._populate_table()

        def _clear_filters(self) -> None:
            controller.clear_filters()
            self.filter_edit.blockSignals(True)
            self.filter_edit.clear()
            self.filter_edit.blockSignals(False)
            self.hide_stamped_chk.blockSignals(True)
            self.hide_stamped_chk.setChecked(False)
            self.hide_stamped_chk.blockSignals(False)
            update_prefs({"hide_stamped": False})
            self.table.clearSelection()
            self._set_bucket("all")

        def _on_hide_stamped(self, checked: bool) -> None:
            controller.hide_stamped = checked
            update_prefs({"hide_stamped": checked})
            self._populate_table()

        def _run_scan(self, *, selected_only: bool, force: bool = False) -> None:
            self.refresh_calibre_selection()
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
                lambda: controller.scan(ids, progress=_prog, force=force),
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
            stats = controller.scan_stats
            if "reused" in stats:
                self.hero_sub.setText(self.hero_sub.text() +
                    f" · {stats['reused']} cached, {stats['matched']} rematched")
            if controller.scan_cache and controller.scan_cache.error:
                self.hero_sub.setText(self.hero_sub.text() + " · Cache save failed: " + controller.scan_cache.error)
            self.apply_btn.setEnabled(ready_count > 0 and not controller.cache_needs_check)
            self._populate_table()
            self._reflow_controls()

        def _on_detail_columns(self, visible: bool) -> None:
            self.table.setColumnHidden(4, not visible)
            self.table.setColumnHidden(6, not visible)

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
            parts = ["+identifiers"] if row.get("add") else []
            for field in ("title", "authors", "series", "synopsis", "tags", "cover"):
                if field in (row.get("proposals") or {}):
                    parts.append(f"+{field}")
            text = ", ".join(parts) or "No changes"
            return f"Updated · {text}" if row.get("applied_at") else text

        def _proposal_tooltip(self, row: dict[str, Any]) -> str:
            identifiers = [f"{k}: {v}" for k, v in sorted((row.get("add") or {}).items())]
            lines = [*identifiers, self._proposal_text(row)]
            if row.get("matched_catalog_version"):
                lines.append(f"Saved match catalogue: {row['matched_catalog_version']}")
                lines.append(f"Active catalogue: {controller.catalog_version or 'checked on Apply'}")
                lines.append("Selected books are rechecked before writing.")
            if row.get("applied_at"):
                lines.append(f"Updated: {row['applied_at']}")
                lines.append("Fields written: " + ", ".join(row.get("updated_fields") or []))
            return "\n".join(lines)

        def _populate_table_items(self, *, proposals_only: bool = False) -> None:
            keep = self._selected_bids()
            shown = controller.filtered_rows()
            if proposals_only:
                for r_idx, row in enumerate(shown):
                    item = self.table.item(r_idx, 5)
                    text = self._proposal_text(row)
                    if item is not None and item.text() != text:
                        item.setText(text)
                    if item is not None:
                        item.setToolTip(self._proposal_tooltip(row))
                return
            if controller.rows:
                empty_message = "No books match these filters. Use Clear Filters to show the loaded books."
            elif self._scan_completed:
                empty_message = "No books were found in this scan. Add books in Calibre or scan the full library."
            else:
                empty_message = "No books scanned yet. Use Scan Selected or Scan Changes to find matches."
            self.table_empty_lbl.setText(empty_message)
            self.table_empty_lbl.setVisible(not shown)
            self.table.setRowCount(len(shown))
            for r_idx, row in enumerate(shown):
                bid_item = QTableWidgetItem(str(row["id"]))
                bid_item.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
                self.table.setItem(r_idx, 0, bid_item)
                self.table.setItem(r_idx, 1, QTableWidgetItem(str(row.get("title") or "")))
                authors = str(row.get("authors") or "")
                names = [name.strip() for name in authors.split(";") if name.strip()]
                author_text = "; ".join(names[:2])
                if len(names) > 2:
                    author_text += f"; … (+{len(names) - 2})"
                self.table.setItem(r_idx, 2, QTableWidgetItem(author_text))

                b_style = BUCKET_STYLE.get(row["bucket"], BUCKET_STYLE["not_in_blc"])
                label = b_style["label"]
                matched_version = row.get("matched_catalog_version")
                if matched_version and controller.catalog_version and matched_version != controller.catalog_version:
                    label = "Saved · " + label
                b_item = QTableWidgetItem(label)
                self.table.setItem(r_idx, 3, b_item)

                wid_text = str(row.get("work_id") or "None")
                self.table.setItem(r_idx, 4, QTableWidgetItem(wid_text))

                self.table.setItem(r_idx, 5, QTableWidgetItem(self._proposal_text(row)))
                self.table.setItem(r_idx, 6, QTableWidgetItem(str(row.get("reason") or "")))
                for column in range(self.table.columnCount()):
                    item = self.table.item(r_idx, column)
                    tooltip = authors if column == 2 else item.text()
                    if column == 3 and matched_version:
                        tooltip += f"\nMatched against catalogue {matched_version}. Rechecked on Apply."
                    if column == 5:
                        tooltip = self._proposal_tooltip(row)
                    item.setToolTip(tooltip)

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
            visible = sum(1 for r in controller.filtered_rows() if is_applicable(r))
            if not self._busy:
                selected_ready = any(int(row["id"]) in selected and is_applicable(row)
                                     for row in controller.filtered_rows())
                self.apply_btn.setEnabled(not controller.cache_needs_check
                                          and bool(selected_ready if selected else visible))
            if selected:
                self.apply_btn.setText(f"Apply Selected ({len(selected)})")
            else:
                visible = sum(1 for r in controller.filtered_rows() if is_applicable(r))
                self.apply_btn.setText(f"Apply Visible Matches ({visible})")

        def _current_row(self) -> dict[str, Any] | None:
            items = self.table.selectedItems()
            if not items:
                return None
            current = self.table.currentItem()
            r_idx = current.row() if current is not None and current.isSelected() else items[0].row()
            id_item = self.table.item(r_idx, 0)
            if id_item is None:
                return None
            bid = int(id_item.data(Qt.ItemDataRole.UserRole))
            for row in controller.rows:
                if int(row["id"]) == bid:
                    return row
            return None

        def _on_row_selected(self) -> None:
            row = self._current_row()
            self.show_calibre_btn.setEnabled(row is not None and not self._retired)
            if row is not None and row.get("cover_receipt") and not self._busy:
                controller.validate_cover_receipt(int(row["id"]))
                selected_items = self.table.selectedItems()
                if selected_items:
                    current = self.table.currentItem()
                    r_idx = current.row() if current is not None and current.isSelected() else selected_items[0].row()
                    proposal_item = self.table.item(r_idx, 5)
                    if proposal_item is not None:
                        proposal_item.setText(self._proposal_text(row))
                        proposal_item.setToolTip(self._proposal_tooltip(row))
            self._update_apply_label()
            self._update_pick_enabled()
            if row is None:
                self.calibre_link_status.clear()
                self.calibre_link_status.setVisible(False)
                self.detail_view.setPlainText("Select a book to inspect its match and proposed updates.")
                return
            self.detail_view.setHtml(render_work_detail_html(row, row.get("work")))
            if not self.pick_search.text().strip():
                self.pick_search.setPlaceholderText(f"Search BLC for '{row.get('title')}'\u2026")

        def _on_current_book_changed(self, row_index: int, *_args) -> None:
            item = self.table.item(row_index, 0)
            if item is not None:
                self._show_in_calibre(book_id=int(item.data(Qt.ItemDataRole.UserRole)))
                self._on_row_selected()

        def _show_in_calibre(self, *, book_id: int | None = None, activate: bool = False) -> None:
            if self._retired or gui.current_db is not library_db:
                return
            row = self._current_row()
            if book_id is None:
                if row is None:
                    return
                book_id = int(row["id"])
            try:
                view = gui.library_view
                # select_rows clears the existing selection even when an ID is hidden.
                if book_id not in view.ids_to_rows([book_id]):
                    self.calibre_link_status.setText(
                        f"Book {book_id} is hidden or removed in Calibre. Clear its search or switch virtual library to show it."
                    )
                    self.calibre_link_status.setVisible(True)
                    return
                if view.current_id != book_id or view.get_selected_ids() != [book_id]:
                    view.select_rows([book_id], using_ids=True)
                self.calibre_link_status.setText(f"Book {book_id} selected in Calibre.")
                self.calibre_link_status.setVisible(True)
                if activate:
                    if gui.isMinimized():
                        gui.showNormal()
                    gui.raise_()
                    gui.activateWindow()
                    view.setFocus()
            except (AttributeError, KeyError, RuntimeError, TypeError, ValueError) as error:
                self.calibre_link_status.setText(f"Could not select book {book_id} in Calibre: {error}")
                self.calibre_link_status.setVisible(True)

        def _update_pick_enabled(self) -> None:
            idx = self.pick_list.currentRow()
            self.pick_apply_btn.setEnabled(
                not self._busy and not controller.cache_needs_check and self._current_row() is not None
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

        def _on_max_changed(self) -> None:
            cap = int(self.max_spin.value())
            if controller.prefs.get("max_apply") != cap:
                controller.prefs["max_apply"] = cap
                update_prefs({"max_apply": cap})

        def _on_apply(self) -> None:
            self._on_max_changed()
            cap = int(self.max_spin.value())
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

            self._apply_target_count = len(target_ids)
            self.hero_state.setText(f"Applying to {len(target_ids)} book(s)\u2026")
            cancel = self._cancel
            bridge = self._bridge

            def _prog(done: int, total: int, event: dict[str, Any]) -> None:
                bridge.apply_progressed.emit(done, total, dict(event))

            self._start_job(
                "Apply",
                lambda: controller.apply(target_ids, max_books=cap, should_stop=cancel.is_set, progress=_prog),
                self._after_apply,
                cancellable=True,
            )

        def _after_apply(self, results: list[dict[str, Any]]) -> None:
            self._after_scan()
            counts = collections.Counter(r["status"] for r in results)
            not_attempted = max(0, getattr(self, "_apply_target_count", len(results)) - len(results))
            self.hero_state.setText(
                f"Apply finished: {counts['written']} written, {counts['refused']} refused, "
                f"{counts['failed']} failed, {counts['cancelled']} cancelled, {not_attempted} not attempted"
            )
            failures = [r for r in results if r["status"] == "failed"]
            if failures:
                failure = failures[0]
                rollback = failure.get("rolled_back")
                recovery = (
                    "No changes were written to this book." if failure.get("stage") == "before_write" else
                    "The failed book's changes were rolled back." if rollback is True else
                    "Rollback could not be confirmed; check this book before retrying."
                )
                QMessageBox.warning(
                    self, "BL-Catalog Apply Stopped",
                    f"Stopped at book {failure['id']}: {failure.get('title', '')}\n\n"
                    f"{failure.get('error', 'Unknown error')}\n{recovery}\n\n"
                    f"{counts['written']} books written; {not_attempted} not attempted.\n"
                    "Earlier successful writes remain saved. See the audit log for details.",
                )
            detail = "Table and saved results refreshed."
            if controller.refresh_error:
                detail = "Writes completed, but table refresh failed: " + controller.refresh_error
            elif controller.scan_cache and controller.scan_cache.error:
                detail = "Table refreshed; cache save failed: " + controller.scan_cache.error
            self.hero_sub.setText(f"{detail} Audit log: {controller.prefs.get('log_path')}")
            with contextlib.suppress(AttributeError, RuntimeError, TypeError):
                gui.library_view.model().refresh_ids([r["id"] for r in results if r["status"] == "written"])

    return _Dialog()

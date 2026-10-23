"""Guarded apply engine for writing BL-Catalog metadata to Calibre.

Preserves every safety invariant from library-finish PR #208 (D078):
- Max-books cap per batch (refuses before any write if exceeded or if duplicate IDs exist).
- Re-classification of each target against the supplied book list, then a live re-read of
  Calibre state: the write is refused if the title, authors, or identifiers changed since the
  books were read.
- Compare-and-set in ``CalibreDbWriter``: fields are written only if the book still matches the
  state the decision was based on, and any partial write is rolled back on failure.
- Duplicate work_id guard across both the existing library AND the current batch
  (so two books in one batch can never both claim the same ``bl_catalog`` work ID).
- Identifiers-only by default (``bl_catalog`` + missing valid ``isbn``), with explicit
  opt-in toggles in ``ApplyOptions`` for title, authors, series, synopsis, tags, and cover.
- Existing valid ISBNs are never overwritten.
- Read-back verification after every book write: checks that updated fields match what was
  sent and that no untouched field drifted; halts the batch immediately on any failure.
- Append-only JSONL audit log with ``flush()`` + ``os.fsync()``. Each entry records the
  previous value of every changed field; replaced covers are saved next to the log.
"""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from .config import DEFAULT_MAX_APPLY, ApplyOptions
from .matcher import (
    APPLICABLE_BUCKETS,
    CERTAIN,
    WORK_ID_RE,
    CatalogueError,
    build_proposals,
    classify,
    cover_of,
    cover_receipt_matches,
)


class ApplyRefused(Exception):
    """The whole apply batch is refused before any write occurs."""


class ReadBackMismatch(RuntimeError):
    def __init__(self, field: str, expected: Any, actual: Any) -> None:
        super().__init__(f"read-back: {field} differs from what was sent")
        self.verification = {"field": field, "expected": expected, "actual": actual}


class StaleBookError(RuntimeError):
    """The live Calibre book no longer matches the state the apply decision was based on."""


IDENTITY_FIELDS = ("title", "authors", "identifiers")


def _identity(state: dict[str, Any]) -> tuple[Any, ...]:
    idents = state.get("identifiers") or {}
    if not isinstance(idents, dict):
        idents = {k: v for k, v in idents}
    return (
        str(state.get("title") or ""),
        [str(a) for a in state.get("authors") or ()],
        {str(k): str(v) for k, v in idents.items()},
    )


def read_id_list(path: str | Path) -> list[int]:
    """Read Calibre book IDs from a text file (one per line, '#' starts a comment)."""
    ids: list[int] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        cleaned = line.split("#", 1)[0].strip()
        if cleaned:
            ids.append(int(cleaned))
    return ids


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def append_log(path: str | Path | None, entry: dict[str, Any]) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class CalibreDbWriter:
    """Adapter around Calibre's ``db.new_api`` implementing the guarded Writer interface."""

    def __init__(self, new_api: Any) -> None:
        self.db = new_api

    def available(self) -> bool:
        return self.db is not None

    def read_books(self, book_ids: list[int] | tuple[int, ...] | None = None) -> list[dict[str, Any]]:
        ids = sorted(book_ids) if book_ids is not None else sorted(self.db.all_book_ids())
        books: list[dict[str, Any]] = []
        for bid in ids:
            st = self._book_dict(int(bid))
            if st is not None:
                books.append(st)
        return books

    def _book_dict(self, bid: int) -> dict[str, Any] | None:
        has_id = getattr(self.db, "has_id", None)
        if callable(has_id) and not has_id(bid):
            return None
        try:
            title = self.db.field_for("title", bid) or ""
            authors = list(self.db.field_for("authors", bid) or ())
            identifiers = dict(self.db.field_for("identifiers", bid) or {})
            formats = list(self.db.formats(bid, verify_formats=False) or ())
            series = self.db.field_for("series", bid) or ""
            series_index = self.db.field_for("series_index", bid)
            comments = self.db.field_for("comments", bid) or ""
            tags = list(self.db.field_for("tags", bid) or ())
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        return {
            "id": int(bid),
            "title": str(title),
            "authors": [str(a) for a in authors],
            "identifiers": {str(k): str(v) for k, v in identifiers.items() if v is not None},
            "formats": [str(f) for f in formats],
            "series": str(series or ""),
            "series_index": float(series_index) if series_index is not None else None,
            "comments": str(comments or ""),
            "tags": [str(t) for t in tags],
        }

    def state(self, bid: int) -> dict[str, Any]:
        book = self._book_dict(int(bid))
        if book is None:
            raise KeyError(f"Book {bid} not found in Calibre database")
        return {
            "state": {
                "title": book["title"],
                "authors": list(book["authors"]),
                "series": book["series"],
                "series_index": book["series_index"],
                "comments": book["comments"],
                "tags": list(book["tags"]),
                "identifiers": [[k, v] for k, v in sorted(book["identifiers"].items())],
            }
        }

    def cover_bytes(self, bid: int) -> bytes | None:
        """Current cover bytes for ``bid`` (``None`` when the book has no cover)."""
        try:
            data = self.db.cover(int(bid))
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        return bytes(data) if data else None

    def set_fields(
        self,
        bid: int,
        changes: dict[str, Any],
        expected: dict[str, Any] | None = None,
        cover_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        """Write ``changes`` only if the book still equals ``expected``; undo partial writes.

        Calibre has no multi-field transaction, so this re-reads the book immediately before
        writing and restores every field already written if a later write raises.
        """
        bid = int(bid)
        before = self.state(bid)["state"]
        if expected is not None and before != expected:
            raise StaleBookError(f"book {bid} changed in Calibre since it was read; rescan and retry")
        old_cover = self.cover_bytes(bid) if cover_bytes is not None else None
        written: list[str] = []
        cover_written = False
        try:
            for field_name, val in changes.items():
                self.db.set_field(field_name, {bid: val})
                written.append(field_name)
            if cover_bytes is not None:
                self.db.set_cover({bid: cover_bytes})
                cover_written = True
        except Exception:
            self._restore(bid, before, written, old_cover if cover_written else None, cover_written)
            raise
        self._last_backup = (bid, before, list(changes), old_cover, cover_bytes is not None)
        return {"ok": True, "after": self.state(bid)["state"]}

    def restore_last(self, bid: int) -> bool:
        """Undo the most recent successful ``set_fields`` for ``bid`` (used after a bad read-back)."""
        backup = getattr(self, "_last_backup", None)
        if not backup or backup[0] != int(bid):
            return False
        _, before, fields, old_cover, had_cover = backup
        self._restore(int(bid), before, fields, old_cover, had_cover)
        self._last_backup = None
        return True

    def _restore(
        self,
        bid: int,
        before: dict[str, Any],
        fields: list[str],
        old_cover: bytes | None,
        restore_cover: bool,
    ) -> None:
        for field_name in reversed(fields):
            val = before.get(field_name)
            if field_name == "identifiers":
                val = {k: v for k, v in (val or [])}
            self.db.set_field(field_name, {bid: val})
        if restore_cover:
            self.db.set_cover({bid: old_cover})


def _normalize_targets(targets: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in targets:
        if isinstance(item, int) and not isinstance(item, bool):
            normalized.append({"id": item, "picked_work_id": None, "cover_receipt": None})
        elif isinstance(item, dict) and isinstance(item.get("id"), int):
            picked_wid = item.get("picked_work_id") or (item.get("work_id") if item.get("picked") else None)
            normalized.append({"id": int(item["id"]), "picked_work_id": picked_wid,
                               "cover_receipt": item.get("cover_receipt")})
        else:
            raise ApplyRefused(f"Invalid target entry: {item!r}")
    return normalized


def apply_matches(
    targets: list[Any],
    *,
    max_books: int = DEFAULT_MAX_APPLY,
    books: list[dict[str, Any]],
    lookups: Any,
    writer: Any,
    log_path: str | Path | None = None,
    options: ApplyOptions | None = None,
    should_stop: Any = None,
    progress: Any = None,
) -> list[dict[str, Any]]:
    """Re-classify and apply BLC updates to ``targets`` with read-back verification.

    ``should_stop`` is an optional zero-argument callable checked before each book; when it
    returns true the remaining books are recorded as ``cancelled`` and nothing more is written.
    ``progress(done, total, event)`` reports completed results and the current book's phase.
    """
    opts = options or ApplyOptions()
    if not opts.any_enabled():
        raise ApplyRefused("No metadata fields are selected for apply")
    items = _normalize_targets(targets)
    if not items:
        raise ApplyRefused("the target list has no ids")
    ids = [item["id"] for item in items]
    if len(set(ids)) != len(ids):
        raise ApplyRefused("the list file repeats an id")
    if len(ids) > max_books:
        raise ApplyRefused(f"{len(ids)} ids is over --max {max_books}")
    if not writer.available():
        raise ApplyRefused("writer is not write-ready")

    by_id: dict[int, dict[str, Any]] = {int(b["id"]): b for b in books}
    holders: dict[str, set[int]] = collections.defaultdict(set)
    for b in books:
        existing_wid = (b.get("identifiers") or {}).get("bl_catalog")
        if existing_wid:
            holders[str(existing_wid)].add(int(b["id"]))

    results: list[dict[str, Any]] = []
    positions = {bid: index for index, bid in enumerate(ids, 1)}

    def report(phase: str, entry: dict[str, Any]) -> None:
        if progress is None:
            return
        bid = int(entry["id"])
        event = {
            "phase": phase,
            "id": bid,
            "title": str(entry.get("title") or (by_id.get(bid) or {}).get("title") or ""),
            "current": positions[bid],
        }
        try:
            progress(len(results), len(items), event)
        except Exception:
            logging.getLogger(__name__).exception("Could not report Calibre Apply progress")

    def record(entry: dict[str, Any]) -> dict[str, Any]:
        full_entry = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **entry,
        }
        append_log(log_path, full_entry)
        results.append(full_entry)
        report(str(full_entry["status"]), full_entry)
        return full_entry

    # First pass: classify every target live so two books in the same batch claiming the same
    # BLC work are both refused before either is written.
    planned: list[tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]] = []
    batch_claims: dict[str, list[int]] = collections.defaultdict(list)
    for item in items:
        bid = item["id"]
        picked_wid = item["picked_work_id"]
        book = by_id.get(bid)
        report("checking", item)
        if not book:
            planned.append((item, None, None))
            continue
        if hasattr(lookups, "begin"):
            lookups.begin()
        if picked_wid and WORK_ID_RE.fullmatch(str(picked_wid)):
            found_work = lookups.work(str(picked_wid))
            if found_work:
                proposals = build_proposals(book, found_work, opts, picked=True)
                row = {
                    "bucket": "picked",
                    "reason": f"Owner picked {found_work['id']}",
                    "work_id": found_work["id"],
                    "work": found_work,
                    "add": dict(proposals.get("identifiers") or {"bl_catalog": found_work["id"]}),
                    "proposals": proposals,
                    "picked": True,
                }
            else:
                row = {
                    "bucket": "error",
                    "reason": f"Picked work {picked_wid} not found in BLC",
                    "work_id": "",
                    "work": None,
                    "add": {},
                    "proposals": {},
                    "picked": True,
                }
        else:
            row = classify(book, lookups)
            work = row.get("work")
            if row["bucket"] in CERTAIN and work:
                row["proposals"] = build_proposals(book, work, opts, picked=False)
            elif row["bucket"] == "already" and work:
                # Confirmed or merged stamp: refresh selected metadata, and move the stamp to the
                # surviving work when BLC merged the old one.
                with_id = bool((row.get("add") or {}).get("bl_catalog"))
                row["proposals"] = build_proposals(book, work, opts, picked=False, with_id=with_id)
            else:
                row["proposals"] = {}
            if "identifiers" in row["proposals"]:
                row["add"] = dict(row["proposals"]["identifiers"])
            elif row["bucket"] == "already":
                row["add"] = {}
        wid = row.get("work_id") or ""
        if wid and row["bucket"] in APPLICABLE_BUCKETS + ("already",):
            batch_claims[wid].append(bid)
        planned.append((item, book, row))

    for index, (item, book, row) in enumerate(planned):
        bid = item["id"]
        if should_stop is not None and should_stop():
            for later_item, later_book, later_row in planned[index:]:
                record({
                    "id": later_item["id"],
                    "title": (later_book or {}).get("title") or "",
                    "bucket": (later_row or {}).get("bucket") or "",
                    "work_id": (later_row or {}).get("work_id") or "",
                    "status": "cancelled",
                    "error": "apply cancelled before this book was written",
                })
            break
        if book is None or row is None:
            record({"id": bid, "status": "refused", "error": "no such Calibre book"})
            continue

        wid = row.get("work_id") or ""
        base = {
            "id": bid,
            "title": book.get("title") or "",
            "bucket": row["bucket"],
            "work_id": wid,
        }
        allowed_buckets = APPLICABLE_BUCKETS + ("already",)
        if row["bucket"] not in allowed_buckets or not wid:
            record({**base, "status": "refused", "error": f"bucket is {row['bucket']}: {row['reason']}"})
            continue

        other_holders = (holders.get(wid, set()) | set(batch_claims.get(wid, ()))) - {bid}
        if other_holders:
            record({
                **base,
                "status": "refused",
                "error": f"work {wid} is already on or claimed by another Calibre book",
            })
            continue

        report("checking", base)
        try:
            state = writer.state(bid)["state"]
        except Exception as error:  # noqa: BLE001 - preserve earlier successes when a live state read fails
            record({
                **base,
                "status": "failed",
                "stage": "before_write",
                "error": str(error) or type(error).__name__,
            })
            break
        if _identity(state) != _identity(book):
            record({
                **base,
                "status": "refused",
                "error": "title, authors, or identifiers changed since the books were read; rescan",
            })
            continue
        live_idents = {k: v for k, v in state["identifiers"]}
        if row["bucket"] == "already":
            stamped = (book.get("identifiers") or {}).get("bl_catalog")
            if live_idents.get("bl_catalog") != stamped:
                record({
                    **base,
                    "status": "refused",
                    "error": "the book's bl_catalog identifier changed since it was classified",
                })
                continue
        elif live_idents.get("bl_catalog") and not row.get("picked"):
            record({
                **base,
                "status": "refused",
                "error": "the book already has a bl_catalog identifier",
            })
            continue

        proposals = row.get("proposals") or {}
        changes: dict[str, Any] = {}
        added_idents: dict[str, str] = {}

        if opts.identifiers:
            raw_add = row.get("add") or {}
            filtered_add = {
                k: v
                for k, v in raw_add.items()
                if k == "bl_catalog"
                or (
                    k == "isbn"
                    and not any(t.lower() in ("isbn", "isbn13", "isbn10") for t in live_idents)
                )
            }
            if filtered_add:
                want_idents = {**live_idents, **filtered_add}
                if want_idents != live_idents:
                    changes["identifiers"] = want_idents
                    added_idents = filtered_add

        if opts.title and "title" in proposals and proposals["title"] != state.get("title"):
            changes["title"] = proposals["title"]

        if opts.authors and "authors" in proposals and proposals["authors"] != state.get("authors"):
            changes["authors"] = list(proposals["authors"])

        if opts.series and "series" in proposals:
            ser_info = proposals["series"]
            if ser_info.get("series") and ser_info["series"] != state.get("series"):
                changes["series"] = ser_info["series"]
            if ser_info.get("series_index") is not None and ser_info["series_index"] != state.get("series_index"):
                changes["series_index"] = ser_info["series_index"]

        if opts.synopsis and "synopsis" in proposals and proposals["synopsis"] != state.get("comments"):
            changes["comments"] = proposals["synopsis"]

        if opts.tags and "tags" in proposals and proposals["tags"] != state.get("tags"):
            changes["tags"] = list(proposals["tags"])

        cover_bytes: bytes | None = None
        cover_meta: dict[str, Any] | None = None
        if opts.cover and "cover" in proposals and row.get("work"):
            cov = cover_of(row["work"])
            receipt = item.get("cover_receipt")
            cover_verified = False
            if cover_receipt_matches(receipt, cov) and hasattr(writer, "cover_bytes"):
                try:
                    current_cover = writer.cover_bytes(bid)
                    cover_verified = bool(current_cover) and hashlib.sha256(current_cover).hexdigest() == receipt["stored_sha256"]
                except Exception:  # noqa: BLE001 - an unreadable cover must not justify skipping a write
                    cover_verified = False
            if not cover_verified and cov and cov["good"] and hasattr(lookups, "verified_cover"):
                report("cover", base)
                try:
                    if hasattr(lookups, "begin"):
                        lookups.begin()
                    fetched = lookups.verified_cover(wid, expected_sha256=cov["sha256"])
                except CatalogueError as error:
                    record({**base, "status": "failed", "stage": "before_write", "error": f"cover: {error}"})
                    break
                if fetched:
                    cover_bytes = fetched[0]
                    cover_meta = {"sha256": cov["sha256"], "width": cov["width"], "height": cov["height"]}

        if not changes and cover_bytes is None:
            record({
                **base,
                "status": "refused",
                "error": "no new identifiers or selected metadata fields to write",
            })
            continue

        before_fields = {f: state.get(f) for f in changes}
        cover_backup: str | None = None
        written_ok = False
        try:
            if cover_bytes is not None and hasattr(writer, "cover_bytes"):
                cover_backup = _save_cover_backup(log_path, bid, writer.cover_bytes(bid))
            report("writing", base)
            try:
                reply = writer.set_fields(bid, changes, expected=state, cover_bytes=cover_bytes)
            except TypeError:
                # Backward-compatible with 3-arg FakeWriter.set_fields(bid, changes, expected=state)
                reply = writer.set_fields(bid, changes, expected=state)
            written_ok = True
            report("verifying", base)
            after = writer.state(bid)["state"]
            if "identifiers" in changes and dict(after["identifiers"]) != changes["identifiers"]:
                raise RuntimeError("read-back: identifiers differ from what was sent")
            for field_name, expected_val in changes.items():
                if field_name == "identifiers":
                    continue
                if after.get(field_name) != expected_val:
                    raise ReadBackMismatch(field_name, expected_val, after.get(field_name))
            if any(after.get(f) != state.get(f) for f in state if f not in changes):
                raise RuntimeError("read-back: a field other than the requested fields changed")
        except Exception as error:  # noqa: BLE001 - any writer or read-back failure halts the batch
            rolled_back: bool | None = None
            if written_ok and hasattr(writer, "restore_last"):
                try:
                    rolled_back = bool(writer.restore_last(bid))
                except Exception:  # noqa: BLE001 - report, never mask the original failure
                    rolled_back = False
            failed_entry: dict[str, Any] = {
                **base,
                "status": "failed",
                "added": added_idents,
                "before": before_fields,
                "error": str(error),
            }
            if isinstance(error, ReadBackMismatch):
                failed_entry["verification"] = error.verification
            if rolled_back is not None:
                failed_entry["rolled_back"] = rolled_back
            if cover_backup:
                failed_entry["cover_backup"] = cover_backup
            record(failed_entry)
            break

        holders[wid].add(bid)
        entry_out: dict[str, Any] = {
            **base,
            "status": "written",
            "added": added_idents,
            "before": before_fields,
            "history_id": reply.get("history_id") if isinstance(reply, dict) else None,
            "recovery": reply.get("recovery") if isinstance(reply, dict) else None,
        }
        non_ident_fields = [k for k in changes if k != "identifiers"]
        if cover_meta:
            non_ident_fields.append("cover")
            entry_out["catalogue_cover_sha256"] = cover_meta["sha256"]
        if non_ident_fields:
            entry_out["fields_updated"] = non_ident_fields
        if cover_backup:
            entry_out["cover_backup"] = cover_backup
        record(entry_out)

    return results


def _save_cover_backup(log_path: str | Path | None, bid: int, data: bytes | None) -> str | None:
    """Keep the cover being replaced next to the audit log so it can be restored by hand."""
    if not log_path or not data:
        return None
    folder = Path(log_path).parent / "blc-cover-backups"
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    target = folder / f"book-{int(bid)}-{digest[:16]}.img"
    if not target.exists():
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    return str(target)


def apply_list(
    list_path: str | Path,
    approved: str,
    *,
    max_books: int = 10,
    books: list[dict[str, Any]],
    lookups: Any,
    writer: Any,
    log_path: str | Path,
    options: ApplyOptions | None = None,
) -> list[dict[str, Any]]:
    """PR #208 compatible list-file apply requiring ``--yes-i-approved <sha256>``."""
    if approved != file_sha256(list_path):
        raise ApplyRefused("--yes-i-approved must be the sha256 of the list file")
    ids = read_id_list(list_path)
    return apply_matches(
        ids,
        max_books=max_books,
        books=books,
        lookups=lookups,
        writer=writer,
        log_path=log_path,
        options=options,
    )

"""Calibre Metadata & Cover Source plugin for BL-Catalog (Ctrl+D / fetch-ebook-metadata).

Provides ``BLCatalogMetadataSource``, which subclasses
``calibre.ebooks.metadata.sources.base.Source`` inside Calibre and falls back to a
compatible pure-Python base class outside Calibre for unit testing.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from .client import HttpCatalogClient, Lookups, SnapshotCatalogClient
from .config import get_prefs
from .matcher import (
    WORK_ID_RE,
    CatalogueError,
    authors_of,
    choose_isbn,
    cover_of,
    offered_credits,
    plain_title,
    scalar_value,
    series_of,
    tags_of,
    title_of,
    to_isbn13,
    valid_isbn,
)
from .version import MINIMUM_CALIBRE_VERSION, PLUGIN_VERSION

try:
    from calibre.ebooks.metadata.book.base import Metadata as CalibreMetadata  # type: ignore[import-not-found]
    from calibre.ebooks.metadata.sources.base import Source as CalibreSourceBase  # type: ignore[import-not-found]
except ImportError:

    class CalibreMetadata:  # type: ignore[no-redef]
        """Lightweight fallback matching Calibre's ``Metadata`` API for unit tests."""

        def __init__(self, title: str, authors: list[str] | tuple[str, ...] = ()) -> None:
            self.title = title
            self.authors = list(authors)
            self.series: str | None = None
            self.series_index: float | int | None = None
            self.comments: str | None = None
            self.tags: list[str] = []
            self.publisher: str | None = None
            self.pubdate: datetime | None = None
            self.source_relevance: int = 0
            self.has_cached_cover_url: bool = False
            self._identifiers: dict[str, str] = {}

        def set_identifier(self, kind: str, val: str) -> None:
            if val:
                self._identifiers[str(kind)] = str(val)

        def get_identifiers(self) -> dict[str, str]:
            return dict(self._identifiers)

    class CalibreSourceBase:  # type: ignore[no-redef]
        """Lightweight fallback matching Calibre's ``Source`` API for unit tests."""

        name = "BL-Catalog"
        version = (1, 0, 0)

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._isbn_to_id: dict[str, str] = {}
            self._id_to_cover: dict[str, str] = {}

        def cache_isbn_to_identifier(self, isbn: str, identifier: str) -> None:
            self._isbn_to_id[isbn] = identifier

        def cached_isbn_to_identifier(self, isbn: str) -> str | None:
            return self._isbn_to_id.get(isbn)

        def cache_identifier_to_cover_url(self, identifier: str, url: str) -> None:
            self._id_to_cover[identifier] = url

        def cached_identifier_to_cover_url(self, identifier: str) -> str | None:
            return self._id_to_cover.get(identifier)

        def clean_downloaded_metadata(self, mi: Any) -> None:
            pass


def _parse_pubdate(work: dict[str, Any], primary_edition: dict[str, Any] | None = None) -> datetime | None:
    candidates = []
    if primary_edition:
        candidates.append(scalar_value(primary_edition.get("release_date")))
    candidates.append(scalar_value(work.get("first_published")))
    for raw in candidates:
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = raw.strip()
        try:
            if len(text) == 4 and text.isdigit():
                return datetime(int(text), 1, 2, tzinfo=UTC)
            if len(text) >= 10:
                dt = datetime.fromisoformat(text[:10])
                return dt.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _primary_edition(work: dict[str, Any], matched_isbn: str | None = None) -> dict[str, Any] | None:
    editions = [e for e in (work.get("editions") or []) if isinstance(e, dict)]
    if not editions:
        return None
    if matched_isbn:
        want13 = to_isbn13(matched_isbn)
        for ed in editions:
            if to_isbn13(scalar_value(ed.get("isbn13")) or scalar_value(ed.get("isbn10")) or "") == want13:
                return ed
    best = choose_isbn(work, valid_isbn)
    if best and best.get("edition_id"):
        for ed in editions:
            if ed.get("id") == best["edition_id"]:
                return ed
    return editions[0]


def work_to_calibre_metadata(
    work: dict[str, Any],
    *,
    matched_isbn: str | None = None,
    relevance: int = 0,
) -> CalibreMetadata:
    """Convert a full BL-Catalog work dict into a Calibre ``Metadata`` object."""
    title = plain_title(work.get("title")) or title_of(work) or "Unknown"
    offered, credits_kind = offered_credits(work)
    people = offered if (credits_kind in ("confirmed", "contents") and offered) else authors_of(work)
    if not people and offered:
        people = offered
    mi = CalibreMetadata(title, people or ["Unknown"])
    mi.source_relevance = relevance
    wid = str(work.get("id") or "")
    if wid:
        mi.set_identifier("bl_catalog", wid)

    ed = _primary_edition(work, matched_isbn=matched_isbn)
    isbn_val = None
    if matched_isbn and to_isbn13(matched_isbn) and "isbn_disputed" not in (work.get("flags") or []):
        isbn_val = to_isbn13(matched_isbn)
    elif credits_kind in ("confirmed", "contents"):
        chosen = choose_isbn(work, valid_isbn)
        if chosen:
            isbn_val = chosen["value"]
    if isbn_val:
        mi.set_identifier("isbn", isbn_val)

    ser = series_of(work)
    if ser and ser.get("series"):
        mi.series = ser["series"]
        mi.series_index = ser["series_index"] if ser.get("series_index") is not None else 1

    syn = scalar_value(work.get("synopsis"))
    if isinstance(syn, str) and syn.strip():
        mi.comments = syn.strip()

    tags = tags_of(work)
    if tags:
        mi.tags = tags

    if ed:
        pub = scalar_value(ed.get("publisher")) or scalar_value(ed.get("imprint"))
        if isinstance(pub, str) and pub.strip():
            mi.publisher = pub.strip()
    elif not getattr(mi, "publisher", None):
        mi.publisher = "Black Library"

    pubdate = _parse_pubdate(work, ed)
    if pubdate is not None:
        mi.pubdate = pubdate

    cov = cover_of(work)
    mi.has_cached_cover_url = bool(cov and cov["good"])
    return mi


class BLCatalogMetadataSource(CalibreSourceBase):
    """Calibre Metadata Source plugin for Black Library Catalogue (bl-catalog)."""

    name = "BL-Catalog"
    description = "Black Library, Games Workshop, and Warhammer fiction catalogue (bl-catalog)"
    supported_platforms: ClassVar[list[str]] = ["windows", "osx", "linux"]
    author = "BL-Catalog"
    version = PLUGIN_VERSION
    minimum_calibre_version = MINIMUM_CALIBRE_VERSION

    capabilities = frozenset(["identify", "cover"])
    touched_fields = frozenset([
        "title",
        "authors",
        "identifier:bl_catalog",
        "identifier:isbn",
        "series",
        "series_index",
        "comments",
        "tags",
        "publisher",
        "pubdate",
    ])
    has_html_comments = False
    supports_gzip_transfer_encoding = False
    prefer_results_with_isbn = True

    def __init__(self, *args: Any, lookups: Lookups | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._injected_lookups = lookups
        self._cover_sha_by_work: dict[str, str] = {}

    def _build_lookups(self, timeout: float = 15.0) -> Lookups:
        if self._injected_lookups is not None:
            return self._injected_lookups
        prefs = get_prefs()
        http_client = HttpCatalogClient(
            prefs.get("url"),
            token=prefs.get("token"),
            timeout=float(timeout or prefs.get("timeout") or 10.0),
        )
        snap_path = prefs.get("snapshot_path")
        if prefs.get("mode") == "snapshot" and snap_path and Path(snap_path).is_file():
            with contextlib.suppress(CatalogueError, OSError):
                return Lookups(SnapshotCatalogClient(snap_path, http_client=http_client))
        return Lookups(http_client)

    def is_configured(self) -> bool:
        prefs = get_prefs()
        return bool((prefs.get("url") or "").strip())

    def get_book_url(self, identifiers: dict[str, Any]) -> tuple[str, str, str] | None:
        wid = (identifiers or {}).get("bl_catalog")
        if isinstance(wid, str) and WORK_ID_RE.fullmatch(wid):
            base = (get_prefs().get("url") or "").rstrip("/")
            if base:
                return ("bl_catalog", wid, f"{base}/v1/work/{wid}")
        return None

    def _cache_work_cover(self, work: dict[str, Any], matched_isbn: str | None = None) -> None:
        wid = str(work.get("id") or "")
        if not wid:
            return
        if matched_isbn:
            isbn13 = to_isbn13(matched_isbn)
            if isbn13:
                self.cache_isbn_to_identifier(isbn13, wid)
        cov = cover_of(work)
        if cov and cov["good"]:
            self._cover_sha_by_work[wid] = cov["sha256"]
            self.cache_identifier_to_cover_url(wid, f"{cov['url']}#{cov['sha256']}")

    def identify(
        self,
        log: Any,
        result_queue: Any,
        abort: Any,
        title: str | None = None,
        authors: list[str] | None = None,
        identifiers: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> None:
        idents = identifiers or {}
        lookups = self._build_lookups(timeout=timeout)
        found_works: list[tuple[dict[str, Any], str | None]] = []
        seen_ids: set[str] = set()

        def _add_work(w: dict[str, Any] | None, via_isbn: str | None = None) -> None:
            if not isinstance(w, dict):
                return
            wid = str(w.get("id") or "")
            if wid and wid not in seen_ids:
                seen_ids.add(wid)
                found_works.append((w, via_isbn))

        try:
            blc_id = idents.get("bl_catalog")
            if isinstance(blc_id, str) and WORK_ID_RE.fullmatch(blc_id):
                _add_work(lookups.work(blc_id))

            if getattr(abort, "is_set", lambda: False)():
                return

            raw_isbn = idents.get("isbn")
            isbn13 = to_isbn13(str(raw_isbn)) if raw_isbn else None
            if isbn13:
                ed_work = lookups.edition(isbn13)
                if ed_work and "isbn_disputed" not in (ed_work.get("flags") or []):
                    full = lookups.work(ed_work["id"]) or ed_work
                    _add_work(full, via_isbn=isbn13)

            if not found_works and title and str(title).strip():
                clean_authors = [
                    a for a in (authors or [])
                    if isinstance(a, str) and a.strip() and a.strip().lower() != "unknown"
                ]
                for w in lookups.works(str(title).strip(), clean_authors):
                    _add_work(w)
                if not found_works:
                    for w in lookups.search_discovery(str(title).strip(), limit=5):
                        _add_work(w)

            for idx, (work, via_isbn) in enumerate(found_works):
                if getattr(abort, "is_set", lambda: False)():
                    return
                self._cache_work_cover(work, matched_isbn=via_isbn)
                mi = work_to_calibre_metadata(work, matched_isbn=via_isbn, relevance=idx)
                self.clean_downloaded_metadata(mi)
                result_queue.put(mi)
        except Exception:
            if log is not None and hasattr(log, "exception"):
                log.exception("BL-Catalog identify failed")

    def download_cover(
        self,
        log: Any,
        result_queue: Any,
        abort: Any,
        title: str | None = None,
        authors: list[str] | None = None,
        identifiers: dict[str, Any] | None = None,
        timeout: int = 30,
        get_best_cover: bool = False,
    ) -> None:
        idents = identifiers or {}
        lookups = self._build_lookups(timeout=timeout)
        wid = idents.get("bl_catalog")
        if not (isinstance(wid, str) and WORK_ID_RE.fullmatch(wid)):
            raw_isbn = idents.get("isbn")
            isbn13 = to_isbn13(str(raw_isbn)) if raw_isbn else None
            if isbn13:
                wid = self.cached_isbn_to_identifier(isbn13)
                if not wid:
                    ed_work = lookups.edition(isbn13)
                    if ed_work and "isbn_disputed" not in (ed_work.get("flags") or []):
                        wid = ed_work.get("id")
        if not (isinstance(wid, str) and WORK_ID_RE.fullmatch(wid)):
            return
        if getattr(abort, "is_set", lambda: False)():
            return

        expected_sha = self._cover_sha_by_work.get(wid)
        if not expected_sha:
            cached_url = self.cached_identifier_to_cover_url(wid)
            if isinstance(cached_url, str) and "#" in cached_url:
                expected_sha = cached_url.rsplit("#", 1)[-1]
        if not expected_sha:
            work = lookups.work(wid)
            cov = cover_of(work) if work else None
            if not cov or not cov["good"]:
                return
            expected_sha = cov["sha256"]

        try:
            fetched = lookups.verified_cover(wid, expected_sha256=expected_sha)
            if fetched and fetched[0]:
                result_queue.put((self, fetched[0]))
        except Exception:
            if log is not None and hasattr(log, "exception"):
                log.exception("BL-Catalog cover download failed for %s", wid)

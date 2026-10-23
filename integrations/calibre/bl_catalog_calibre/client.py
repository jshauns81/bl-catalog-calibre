"""BL-Catalog client supporting both live HTTP (/v1/*) and local SQLite snapshots.

- ``HttpCatalogClient``: bounded, redirect-refusing, proxy-ignoring HTTP client for ``/v1/*``
  reads, cover byte downloads (with SHA-256 verification), and ``/v1/snapshots/latest.sqlite``
  downloads.
- ``SnapshotCatalogClient``: opens a local BL-Catalog SQLite snapshot read-only
  (``file:...?mode=ro``) and reconstructs the exact ``/v1/work/{id}``, ``/v1/isbn/{isbn}``,
  and ``/v1/search`` payloads locally for instant full-library scans.
- ``Lookups``: unified lookup facade used by ``matcher.py``, ``apply.py``, ``source.py``,
  and ``dialog.py``.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import re
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .matcher import (
    WORK_ID_RE,
    CatalogueError,
    calibre_title_norm,
    norm,
    plain_title,
    to_isbn13,
)

MAX_RESPONSE = 2 * 1024 * 1024
MAX_IMAGE = 20 * 1024 * 1024
MAX_SNAPSHOT = 512 * 1024 * 1024
IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COVER_ROUTE_RE = re.compile(r"/v1/work/w_[A-Za-z0-9_]{1,80}/cover(?:/(?:original|upscaled))?")

DEFAULT_TIMEOUT = 10.0
BOOK_REQUESTS = 40
BOOK_SECONDS = 30.0
SEARCH_LIMIT = 5
SQLITE_MAGIC = b"SQLite format 3\x00"

IDENTIFIERS = {
    "isbn": ("/v1/isbn/", re.compile(r"\d{9}[\dX]|\d{13}")),
    "asin": ("/v1/asin/", re.compile(r"B0[A-Z0-9]{8}")),
}

_WORK_SCALARS = (
    "title",
    "sort_title",
    "type",
    "setting",
    "synopsis",
    "word_count",
    "length_class",
    "first_published",
)
_EDITION_SCALARS = (
    "format",
    "isbn10",
    "isbn13",
    "asin",
    "audible_asin",
    "publisher",
    "imprint",
    "release_date",
    "region",
    "pages",
    "runtime_minutes",
    "cover_artist",
    "cover_url",
    "price",
    "price_currency",
    "in_print",
)
_BOOL_FIELDS = frozenset({"in_print"})
_LOW_SCORE = 0.5
_AUTHOR_ORDER = "CASE wa.role WHEN 'author' THEN 0 WHEN 'editor' THEN 1 WHEN 'narrator' THEN 2 ELSE 3 END"
_SNAPSHOT_COLUMNS = {
    "meta": {"key", "value"},
    "work": {"id", *_WORK_SCALARS},
    "edition": {"id", "work_id", *_EDITION_SCALARS},
    "work_alias": {"work_id", "title"},
    "author": {"id", "name", "sort_name"},
    "work_author": {"work_id", "author_id", "role"},
    "series": {"id", "name"},
    "work_series": {"work_id", "series_id", "position"},
    "work_tag": {"work_id", "kind", "value"},
    "work_ident": {"work_id", "kind", "value"},
    "edition_ident": {"edition_id", "kind", "value", "status"},
    "field_support": {"entity", "entity_id", "field", "value", "score", "agree_json", "disagree_json"},
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise CatalogueError("bl-catalog redirect refused")


def configured_url(url: str | None = None) -> str:
    """Validate and normalize the BL-Catalog base URL."""
    raw = (url if url is not None else os.environ.get("BL_CATALOG_URL", "")).strip()
    if not raw:
        raise CatalogueError("bl-catalog URL is not configured")
    parts = urllib.parse.urlsplit(raw)
    try:
        _ = parts.port
    except ValueError as exc:
        raise CatalogueError("BL_CATALOG_URL has an invalid port") from exc
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise CatalogueError("BL_CATALOG_URL must be a plain http(s) origin")
    return raw.rstrip("/")


def build_safe_opener():
    """Return an opener that ignores environment proxies and refuses redirects."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class HttpCatalogClient:
    """Bounded HTTP client for a BL-Catalog server."""

    def __init__(
        self,
        url: str | None = None,
        *,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        opener=None,
    ) -> None:
        self.url = configured_url(url)
        self.token = (token or "").strip()
        self.timeout = float(timeout) if timeout and timeout > 0 else DEFAULT_TIMEOUT
        self.opener = opener or build_safe_opener()
        self.version: str | None = None
        self.requests_left = BOOK_REQUESTS
        self.deadline = time.monotonic() + BOOK_SECONDS
        self.begin()

    def begin(self, requests: int | None = None, seconds: float | None = None) -> None:
        """Start one book's request/time budget and reset snapshot version pinning."""
        self.requests_left = BOOK_REQUESTS if requests is None else int(requests)
        self.deadline = time.monotonic() + (BOOK_SECONDS if seconds is None else float(seconds))
        self.version = None

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "Accept-Encoding": "identity",
            "User-Agent": "BL-Catalog-Calibre/1",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _read(self, response, limit: int, deadline: float | None = None) -> bytes:
        read = getattr(response, "read1", None) or response.read
        chunks: list[bytes] = []
        size = 0
        stop_at = deadline if deadline is not None else self.deadline
        while size <= limit:
            if time.monotonic() > stop_at:
                raise CatalogueError("bl-catalog took too many requests or too long")
            chunk = read(min(64 * 1024, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks)

    def _get(self, route: str, version: str | None = None, missing_ok: bool = False) -> dict[str, Any] | None:
        remaining = self.deadline - time.monotonic()
        if self.requests_left <= 0 or remaining <= 0:
            raise CatalogueError("bl-catalog took too many requests or too long for this book")
        self.requests_left -= 1
        try:
            req = urllib.request.Request(self.url + route, headers=self._headers("application/json"))
            with self.opener.open(req, timeout=min(self.timeout, remaining)) as response:
                raw = self._read(response, MAX_RESPONSE)
            if len(raw) > MAX_RESPONSE:
                raise CatalogueError("bl-catalog response exceeds its limit")
            data = json.loads(raw)
        except CatalogueError:
            raise
        except urllib.error.HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            raise CatalogueError("bl-catalog is unavailable or returned unusable data") from error
        except (OSError, ValueError, AttributeError, http.client.HTTPException) as error:
            raise CatalogueError("bl-catalog is unavailable or returned unusable data") from error

        current = data.get("catalog_version") if isinstance(data, dict) else None
        if (
            not isinstance(current, str)
            or not current
            or len(current) > 80
            or (version is not None and current != version)
            or (self.version is not None and current != self.version)
        ):
            raise CatalogueError("bl-catalog snapshot changed or is unavailable")
        self.version = current
        return data

    def fetch_version(self) -> str:
        """Query ``/v1/version`` and return the active ``catalog_version``."""
        self.begin(requests=5, seconds=max(self.timeout, 10.0))
        data = self._get("/v1/version")
        assert data is not None
        return data["catalog_version"]

    def work(self, ident: str) -> dict[str, Any] | None:
        """Return ``{'catalog_version', 'work', 'merged_from'}`` or None for 404."""
        if not isinstance(ident, str) or not WORK_ID_RE.fullmatch(ident):
            raise CatalogueError("Invalid work id")
        data = self._get("/v1/work/" + ident, missing_ok=True)
        if data is None:
            return None
        work = data.get("work")
        if not isinstance(work, dict) or not isinstance(work.get("id"), str) or not WORK_ID_RE.fullmatch(work["id"]):
            raise CatalogueError("bl-catalog returned an invalid work")
        if work["id"] != ident and data.get("merged_into") != work["id"]:
            raise CatalogueError("bl-catalog work does not match the id asked for")
        return {
            "catalog_version": data["catalog_version"],
            "work": work,
            "merged_from": ident if work["id"] != ident else None,
        }

    def edition(self, kind: str, value: str) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
        """Return ``(catalog_version, work, edition)`` for an ISBN or ASIN, or None for 404."""
        if kind == "isbn":
            norm_isbn = to_isbn13(value)
            if norm_isbn:
                value = norm_isbn
        route, shape = IDENTIFIERS.get(kind, (None, None))
        if route is None or not isinstance(value, str) or not shape.fullmatch(value):
            raise CatalogueError("Invalid identifier query")
        data = self._get(route + value, missing_ok=True)
        if data is None:
            return None
        work = data.get("work")
        if not isinstance(work, dict) or not isinstance(work.get("id"), str) or not WORK_ID_RE.fullmatch(work["id"]):
            raise CatalogueError("bl-catalog returned an invalid edition")
        ed = data.get("edition") if isinstance(data.get("edition"), dict) else {}
        if isinstance(ed.get("flags"), list) and "isbn_disputed" in ed["flags"]:
            flags = list(work.get("flags") if isinstance(work.get("flags"), list) else [])
            if "isbn_disputed" not in flags:
                flags.append("isbn_disputed")
            work = {**work, "flags": flags}
        return data["catalog_version"], work, ed

    def works(self, title: str, authors: list[str] | tuple[str, ...] = ()) -> tuple[str, list[dict[str, Any]]]:
        """Return ``(catalog_version, [full_work, ...])`` for works matching ``title``."""
        if not isinstance(title, str) or not (1 <= len(title) <= 300):
            raise CatalogueError("Invalid title query")
        query: dict[str, Any] = {"title": title}
        first = (
            next((a for a in authors if isinstance(a, str) and 1 <= len(a) <= 300), None)
            if isinstance(authors, (list, tuple))
            else None
        )
        if first:
            query["author"] = first
        query["limit"] = SEARCH_LIMIT
        search = self._get("/v1/search?" + urllib.parse.urlencode(query))
        assert search is not None
        rows = search.get("results")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise CatalogueError("bl-catalog returned an invalid search")
        want = calibre_title_norm(plain_title(title))
        exact = [
            row
            for row in rows[:SEARCH_LIMIT]
            if want and calibre_title_norm(plain_title(row.get("title"))) == want
        ]
        found_works: list[dict[str, Any]] = []
        for row in exact:
            ident = row.get("id")
            if not isinstance(ident, str) or not WORK_ID_RE.fullmatch(ident):
                raise CatalogueError("bl-catalog returned an invalid work id")
            reply = self._get("/v1/work/" + ident, search["catalog_version"])
            assert reply is not None
            work = reply.get("work")
            if not isinstance(work, dict) or work.get("id") != ident:
                raise CatalogueError("bl-catalog work does not match its search result")
            found_works.append(work)
        return search["catalog_version"], found_works

    def search_discovery(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Discovery search by ``w_...`` work ID, ISBN, or free-text ``q=`` query."""
        q = (query or "").strip()
        if not q:
            return []
        self.begin(requests=max(limit + 5, 15), seconds=max(self.timeout * 2, 20.0))
        if WORK_ID_RE.fullmatch(q):
            found = self.work(q)
            return [found["work"]] if found else []
        isbn13 = to_isbn13(q)
        if isbn13:
            ed = self.edition("isbn", isbn13)
            if ed:
                return [ed[1]]
        capped = max(1, min(int(limit), 20))
        data = self._get("/v1/search?" + urllib.parse.urlencode({"q": q[:300], "limit": capped}))
        if not data or not isinstance(data.get("results"), list):
            return []
        out: list[dict[str, Any]] = []
        for item in data["results"][:capped]:
            if not isinstance(item, dict):
                continue
            wid = item.get("id")
            if isinstance(wid, str) and WORK_ID_RE.fullmatch(wid):
                reply = self.work(wid)
                if reply and reply.get("work"):
                    out.append(reply["work"])
        return out

    def image(self, route: str) -> dict[str, Any] | None:
        """Fetch one cover image from ``/v1/work/<id>/cover``."""
        if not isinstance(route, str) or not COVER_ROUTE_RE.fullmatch(route):
            raise CatalogueError("Invalid cover address")
        remaining = self.deadline - time.monotonic()
        if self.requests_left <= 0 or remaining <= 0:
            raise CatalogueError("bl-catalog took too many requests or too long for this book")
        self.requests_left -= 1
        try:
            req = urllib.request.Request(
                self.url + route,
                headers=self._headers(", ".join(sorted(IMAGE_TYPES))),
            )
            with self.opener.open(req, timeout=min(self.timeout, remaining)) as response:
                headers = getattr(response, "headers", None) or {}
                ctype = str(headers.get("Content-Type") or "").split(";")[0].strip().lower()
                data = self._read(response, MAX_IMAGE)
                etag = headers.get("ETag")
                version = headers.get("X-Catalog-Version")
        except CatalogueError:
            raise
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise CatalogueError("bl-catalog is unavailable or returned an unusable image") from error
        except (OSError, ValueError, AttributeError, http.client.HTTPException) as error:
            raise CatalogueError("bl-catalog is unavailable or returned an unusable image") from error
        if len(data) > MAX_IMAGE:
            raise CatalogueError("bl-catalog image exceeds its limit")
        if not data or ctype not in IMAGE_TYPES:
            raise CatalogueError("bl-catalog returned something other than an image")
        return {
            "data": data,
            "content_type": ctype,
            "etag": etag if isinstance(etag, str) else None,
            "version": version if isinstance(version, str) else None,
        }

    def verified_cover(self, work_id: str, expected_sha256: str | None = None) -> tuple[bytes, str] | None:
        """Fetch cover bytes for ``work_id`` and verify SHA-256 against ``expected_sha256``."""
        if not isinstance(work_id, str) or not WORK_ID_RE.fullmatch(work_id):
            raise CatalogueError("Invalid work id")
        found = self.image(f"/v1/work/{work_id}/cover")
        if not found:
            return None
        digest = hashlib.sha256(found["data"]).hexdigest()
        if expected_sha256 and digest != expected_sha256.lower():
            # Refresh work metadata once in case the snapshot published a new upscale mid-run
            self.begin(requests=4, seconds=max(self.timeout, 10.0))
            refreshed = self.work(work_id)
            cov = (refreshed or {}).get("work", {}).get("cover")
            new_sha = cov.get("sha256") if isinstance(cov, dict) else None
            if not new_sha or digest != new_sha.lower():
                raise CatalogueError("Cover image SHA-256 did not match work metadata")
        return found["data"], found["content_type"]

    def download_snapshot(self, dest_path: str | Path) -> tuple[Path, str]:
        """Download ``/v1/snapshots/latest.sqlite`` atomically to ``dest_path``."""
        target = Path(dest_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(self.timeout * 12, 120.0)
        try:
            req = urllib.request.Request(
                self.url + "/v1/snapshots/latest.sqlite",
                headers=self._headers("application/vnd.sqlite3, application/octet-stream"),
            )
            with self.opener.open(req, timeout=max(self.timeout * 4, 30.0)) as response:
                headers = getattr(response, "headers", None) or {}
                version = str(headers.get("X-Catalog-Version") or "").strip()
                raw = self._read(response, MAX_SNAPSHOT, deadline=deadline)
        except CatalogueError:
            raise
        except (OSError, ValueError, AttributeError, http.client.HTTPException) as error:
            raise CatalogueError(f"Failed to download BLC snapshot: {error}") from error

        if len(raw) > MAX_SNAPSHOT:
            raise CatalogueError("BLC snapshot exceeds maximum size limit")
        if len(raw) < 100 or not raw.startswith(SQLITE_MAGIC):
            raise CatalogueError("Downloaded snapshot is not a valid SQLite database")

        fd, tmp_name = tempfile.mkstemp(prefix=".blc-snap-", suffix=".part", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            version = _validated_snapshot_version(tmp_name, version)
            os.replace(tmp_name, target)
        except Exception:
            if os.path.exists(tmp_name):
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
            raise

        self.version = version
        return target, version


def open_readonly_sqlite(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite database in strict read-only URI mode."""
    uri = "file:" + urllib.parse.quote(str(Path(path).resolve())) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _validated_snapshot_version(path: str | Path, response_version: str) -> str:
    """Validate the staged download before it can replace the pinned catalogue."""
    conn = open_readonly_sqlite(path)
    try:
        if conn.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
            raise CatalogueError("Downloaded BLC snapshot failed its database integrity check")
        for table, required in _SNAPSHOT_COLUMNS.items():
            columns = {row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            if not required <= columns:
                raise CatalogueError(f"Downloaded BLC snapshot has an incompatible {table} table")
        row = conn.execute("SELECT value FROM meta WHERE key IN ('catalog_version', 'version') "
                           "ORDER BY key='catalog_version' DESC LIMIT 1").fetchone()
        version = row[0] if row else None
        if not isinstance(version, str) or not version.strip() or len(version) > 80:
            raise CatalogueError("Downloaded BLC snapshot has no valid catalogue version")
        if response_version and response_version != version:
            raise CatalogueError("Downloaded BLC snapshot version differs from its response header")
        return version
    except sqlite3.Error as exc:
        raise CatalogueError(f"Downloaded BLC snapshot is not a usable database: {exc}") from exc
    finally:
        conn.close()


def read_calibre_books_readonly(metadata_db_path: str | Path) -> list[dict[str, Any]]:
    """Read every book from Calibre's ``metadata.db`` in read-only mode (PR #208 compatible)."""
    db = open_readonly_sqlite(metadata_db_path)
    try:
        books: dict[int, dict[str, Any]] = {
            int(row["id"]): {
                "id": int(row["id"]),
                "title": row["title"] or "",
                "authors": [],
                "identifiers": {},
                "formats": [],
            }
            for row in db.execute("SELECT id, title FROM books ORDER BY id")
        }
        for row in db.execute(
            "SELECT l.book AS book, a.name AS name FROM books_authors_link l "
            "JOIN authors a ON a.id = l.author ORDER BY l.id"
        ):
            bid = int(row["book"])
            if bid in books:
                books[bid]["authors"].append(str(row["name"]).replace("|", ","))
        for row in db.execute("SELECT book, type, val FROM identifiers ORDER BY id"):
            bid = int(row["book"])
            if bid in books and row["type"] and row["val"] is not None:
                books[bid]["identifiers"].setdefault(str(row["type"]), str(row["val"]))
        for row in db.execute("SELECT book, format FROM data ORDER BY id"):
            bid = int(row["book"])
            if bid in books and row["format"]:
                books[bid]["formats"].append(str(row["format"]))
    finally:
        db.close()
    return list(books.values())


def _loads_list(text: str | None) -> list[Any]:
    try:
        out = json.loads(text) if text else []
    except ValueError:
        return []
    return out if isinstance(out, list) else []


def _series_key_norm(name: str) -> str:
    folded = norm(name)
    for prefix in ("the ", "a ", "an "):
        if folded.startswith(prefix):
            folded = folded[len(prefix):]
            break
    return folded


class SnapshotCatalogClient:
    """Read-only catalogue pinned in one transaction until close, including after a sync."""

    def __init__(
        self,
        snapshot_path: str | Path,
        *,
        http_client: HttpCatalogClient | None = None,
    ) -> None:
        self.snapshot_path = Path(snapshot_path)
        if not self.snapshot_path.is_file():
            raise CatalogueError(f"Snapshot file not found: {self.snapshot_path}")
        self.http_client = http_client
        self._title_index: dict[str, set[str]] | None = None
        self.conn = open_readonly_sqlite(self.snapshot_path)
        self.conn.create_function("fold", 1, norm, deterministic=True)
        try:
            self.conn.execute("BEGIN")
            row = self.conn.execute("SELECT value FROM meta WHERE key IN ('catalog_version', 'version') "
                                    "ORDER BY key='catalog_version' DESC LIMIT 1").fetchone()
            self.version = str(row["value"]) if row and row["value"] else "snapshot"
        except sqlite3.Error as exc:
            self.conn.close()
            raise CatalogueError(f"Invalid BLC snapshot database: {exc}") from exc

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self.conn.close()

    def begin(self, requests: int | None = None, seconds: float | None = None) -> None:
        if self.http_client is not None:
            self.http_client.begin(requests=requests, seconds=seconds)

    def _support(self, entity: str, entity_id: str) -> dict[str, list[sqlite3.Row]]:
        rows: dict[str, list[sqlite3.Row]] = {}
        for r in self.conn.execute(
            "SELECT field, value, score, agree_json, disagree_json FROM field_support "
            "WHERE entity=? AND entity_id=? ORDER BY rowid",
            (entity, entity_id),
        ):
            rows.setdefault(r["field"], []).append(r)
        return rows

    def _scalar(self, field: str, value: Any, support: dict[str, list[sqlite3.Row]]) -> dict[str, Any] | None:
        rows = support.get(field)
        if value is not None and field in _BOOL_FIELDS:
            value = bool(value)
        if not rows:
            if value is None:
                return None
            return {"value": value, "score": None, "agree": [], "disagree": []}
        r = rows[0]
        return {
            "value": value,
            "score": r["score"],
            "agree": _loads_list(r["agree_json"]),
            "disagree": _loads_list(r["disagree_json"]),
        }

    def _resolve_work_id(self, work_id: str) -> tuple[str | None, bool]:
        seen = {work_id}
        current = work_id
        while True:
            if self.conn.execute("SELECT 1 FROM work WHERE id=?", (current,)).fetchone():
                return current, current != work_id
            try:
                row = self.conn.execute("SELECT new_id FROM id_alias WHERE old_id=?", (current,)).fetchone()
            except sqlite3.OperationalError:
                return None, False
            if row is None or row["new_id"] in seen:
                return None, False
            current = row["new_id"]
            seen.add(current)

    def _author_rows(self, work_id: str, roles: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            f"SELECT a.id, a.name, a.sort_name, wa.role FROM work_author wa "
            f"JOIN author a ON a.id = wa.author_id WHERE wa.work_id=? "
            f"ORDER BY {_AUTHOR_ORDER}, wa.rowid",
            (work_id,),
        ).fetchall()
        return [r for r in rows if roles is None or r["role"] in roles]

    def _author_aliases(self, author_id: int) -> list[str]:
        try:
            rows = self.conn.execute(
                "SELECT name FROM author_alias WHERE author_id=? ORDER BY name",
                (author_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [r["name"] for r in rows]

    def _credit_support(
        self, support: dict[str, list[sqlite3.Row]], name: str, aliases: list[str], role: str = "author"
    ) -> tuple[str, float | None]:
        spellings = {name, *aliases}
        best: tuple[str, float | None] = (name, None)
        for r in support.get(role, ()):
            if r["value"] in spellings and (best[1] is None or (r["score"] or 0.0) > best[1]):
                best = (r["value"], r["score"])
        return best

    def _work_flags(self, work_id: str, type_: str | None, support: dict[str, list[sqlite3.Row]]) -> list[str]:
        flags: list[str] = []
        primary = next(iter(self._author_rows(work_id, ("author",))), None)
        key_fields: list[tuple[str, str | None]] = [("title", None), ("type", None)]
        if primary is not None:
            credited, _ = self._credit_support(
                support, primary["name"], self._author_aliases(primary["id"])
            )
            key_fields.append(("author", credited))
        for field, val in key_fields:
            rows = support.get(field, ())
            scores = [r["score"] for r in rows if val is None or r["value"] == val]
            if any(s is not None and s < _LOW_SCORE for s in scores):
                flags.append("low_agreement")
                break
        origins: set[str] = set()
        for rows in support.values():
            for r in rows:
                origins.update(str(o) for o in _loads_list(r["agree_json"]))
                for d in _loads_list(r["disagree_json"]):
                    if isinstance(d, dict) and "origin" in d:
                        origins.add(str(d["origin"]))
        if len(origins) == 1:
            flags.append("single_source")
        try:
            contained = (
                self.conn.execute("SELECT 1 FROM work_contains WHERE child_id=?", (work_id,)).fetchone()
                is not None
            )
        except sqlite3.OperationalError:
            contained = False
        if contained:
            flags.append("contained")
        if type_ in ("short_story", "novella"):
            flags.append("short_form")
        return flags

    def _summary(self, work_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT id, title, type FROM work WHERE id=?", (work_id,)).fetchone()
        if row is None:
            return None
        support = self._support("work", work_id)
        return {
            "id": row["id"],
            "title": row["title"],
            "type": row["type"],
            "authors": list(dict.fromkeys(
                a["name"] for a in self._author_rows(work_id, ("author", "editor"))
            )),
            "flags": self._work_flags(work_id, row["type"], support),
        }

    def _cover_metadata(self, work_id: str) -> dict[str, Any] | None:
        try:
            original = self.conn.execute(
                "SELECT source_url, sha256, width, height, media_type FROM work_cover WHERE work_id=?",
                (work_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if original is None:
            return None
        derivative = None
        try:
            derivative = self.conn.execute(
                "SELECT sha256, width, height, media_type, pipeline FROM cover_derivative "
                "WHERE original_sha256=? ORDER BY rowid DESC LIMIT 1",
                (original["sha256"],),
            ).fetchone()
        except sqlite3.OperationalError:
            derivative = None
        selected = derivative if derivative is not None else original
        return {
            "url": f"/v1/work/{work_id}/cover",
            "source": "lexicanum",
            "source_url": original["source_url"],
            "sha256": selected["sha256"],
            "width": selected["width"],
            "height": selected["height"],
            "upscaled": derivative is not None,
            "pipeline": derivative["pipeline"] if derivative is not None else None,
            "original": {
                "url": f"/v1/work/{work_id}/cover/original",
                "sha256": original["sha256"],
                "width": original["width"],
                "height": original["height"],
            },
        }

    def _edition_view(self, edition_id: str, used: tuple[str, str] | None = None) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM edition WHERE id=?", (edition_id,)).fetchone()
        if row is None:
            return None
        support = self._support("edition", edition_id)
        idents = [
            {"kind": r["kind"], "value": r["value"], "status": r["status"]}
            for r in self.conn.execute(
                "SELECT kind, value, status FROM edition_ident WHERE edition_id=? ORDER BY kind, value",
                (edition_id,),
            )
        ]
        out: dict[str, Any] = {"id": row["id"], "work_id": row["work_id"]}
        for field in _EDITION_SCALARS:
            out[field] = self._scalar(field, row[field], support)
        out["identifiers"] = idents
        disputed = used is not None and any(
            i["status"] == "disputed" and (i["kind"], i["value"]) == used for i in idents
        )
        flags: list[str] = []
        if disputed:
            flags.append("isbn_disputed")
        out["flags"] = flags
        return out

    def _work_view(self, work_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM work WHERE id=?", (work_id,)).fetchone()
        if row is None:
            return None
        support = self._support("work", work_id)
        out: dict[str, Any] = {"id": row["id"]}
        for field in _WORK_SCALARS:
            out[field] = self._scalar(field, row[field], support)
        out["authors"] = []
        for a in self._author_rows(work_id):
            aliases = self._author_aliases(a["id"])
            credited, score = self._credit_support(support, a["name"], aliases, a["role"])
            out["authors"].append({
                "id": a["id"],
                "name": a["name"],
                "sort_name": a["sort_name"],
                "role": a["role"],
                "score": score if a["role"] == "author" else None,
                "credited_as": credited,
                "aliases": aliases,
            })

        def _series_rank(s_row: sqlite3.Row) -> tuple:
            key = _series_key_norm(s_row["name"])
            groups, sc = 0, 0.0
            for r in support.get("series", ()):
                if _series_key_norm(r["value"] or "") == key:
                    groups = max(groups, len(_loads_list(r["agree_json"])))
                    sc = max(sc, r["score"] or 0.0)
            return (-groups, -sc, s_row["position"] is None, s_row["name"], s_row["id"])

        series_rows = self.conn.execute(
            "SELECT s.id, s.name, ws.position FROM work_series ws "
            "JOIN series s ON s.id = ws.series_id WHERE ws.work_id=?",
            (work_id,),
        ).fetchall()
        out["series"] = [
            {"id": s["id"], "name": s["name"], "position": s["position"]}
            for s in sorted(series_rows, key=_series_rank)
        ]
        out["aliases"] = [
            r["title"]
            for r in self.conn.execute(
                "SELECT title FROM work_alias WHERE work_id=? ORDER BY title", (work_id,)
            )
        ]
        out["tags"] = [
            {"kind": r["kind"], "value": r["value"]}
            for r in self.conn.execute(
                "SELECT kind, value FROM work_tag WHERE work_id=? ORDER BY kind, value", (work_id,)
            )
        ]
        out["identifiers"] = [
            {"kind": r["kind"], "value": r["value"]}
            for r in self.conn.execute(
                "SELECT kind, value FROM work_ident WHERE work_id=? ORDER BY kind, value", (work_id,)
            )
        ]
        out["editions"] = [
            self._edition_view(r["id"])
            for r in self.conn.execute(
                "SELECT id FROM edition WHERE work_id=? ORDER BY release_date IS NULL, release_date, id",
                (work_id,),
            )
        ]
        try:
            child_rows = self.conn.execute(
                "SELECT wc.child_id, wc.position FROM work_contains wc JOIN work w ON w.id = wc.child_id "
                "WHERE wc.parent_id=? ORDER BY wc.position IS NULL, wc.position, w.title, w.id",
                (work_id,),
            ).fetchall()
            out["contents"] = [
                {**(self._summary(r["child_id"]) or {}), "position": r["position"]}
                for r in child_rows
            ]
        except sqlite3.OperationalError:
            out["contents"] = []
        out["cover"] = self._cover_metadata(work_id)
        out["flags"] = self._work_flags(work_id, row["type"], support)
        return out

    def work(self, ident: str) -> dict[str, Any] | None:
        if not isinstance(ident, str) or not WORK_ID_RE.fullmatch(ident):
            raise CatalogueError("Invalid work id")
        final, merged = self._resolve_work_id(ident)
        if final is None:
            return None
        w = self._work_view(final)
        if w is None:
            return None
        return {
            "catalog_version": self.version,
            "work": w,
            "merged_from": ident if merged else None,
        }

    def edition(self, kind: str, value: str) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
        if kind == "isbn":
            isbn13 = to_isbn13(value)
            if not isbn13:
                return None
            kinds = ("isbn13", "isbn13")
            val = isbn13
            col_sql = "SELECT id FROM edition WHERE isbn13=?"
            col_params: tuple[Any, ...] = (val,)
        elif kind == "asin":
            val = value.strip().upper()
            kinds = ("asin", "audible_asin")
            col_sql = "SELECT id FROM edition WHERE asin=? OR audible_asin=?"
            col_params = (val, val)
        else:
            raise CatalogueError("Invalid identifier kind")

        found: dict[str, tuple[str, str]] = {}
        for r in self.conn.execute(
            "SELECT edition_id, status, kind FROM edition_ident WHERE kind IN (?, ?) AND value=? "
            "ORDER BY status = 'disputed', edition_id",
            (kinds[0], kinds[1], val),
        ):
            found.setdefault(r["edition_id"], (r["status"], r["kind"]))
        for r in self.conn.execute(col_sql, col_params):
            found.setdefault(r["id"], ("ok", kinds[0]))
        if not found:
            return None
        ordered = sorted(found.items(), key=lambda item: (item[1][0] != "ok", item[0]))
        top_eid, (_status, used_kind) = ordered[0]
        ed = self._edition_view(top_eid, used=(used_kind, val))
        if not ed or not ed.get("work_id"):
            return None
        work_id, _ = self._resolve_work_id(ed["work_id"])
        if not work_id:
            return None
        w = self._work_view(work_id)
        if not w:
            return None
        if "isbn_disputed" in (ed.get("flags") or []):
            flags = list(w.get("flags") or [])
            if "isbn_disputed" not in flags:
                flags.append("isbn_disputed")
            w = {**w, "flags": flags}
        return self.version, w, ed

    def works(self, title: str, authors: list[str] | tuple[str, ...] = ()) -> tuple[str, list[dict[str, Any]]]:
        want = calibre_title_norm(plain_title(title))
        if not want:
            return self.version, []
        if self._title_index is None:
            index: dict[str, set[str]] = {}
            for row in self.conn.execute(
                "SELECT id, title FROM work UNION ALL SELECT work_id AS id, title FROM work_alias"
            ):
                key = calibre_title_norm(plain_title(row["title"]))
                if key:
                    index.setdefault(key, set()).add(row["id"])
            self._title_index = index
        matches: list[dict[str, Any]] = []
        for wid in sorted(self._title_index.get(want, ())):
            w = self._work_view(wid)
            if not w:
                continue
            w_title = calibre_title_norm(plain_title(w.get("title")))
            w_aliases = [calibre_title_norm(plain_title(a)) for a in (w.get("aliases") or [])]
            if w_title == want or want in w_aliases:
                matches.append(w)
        return self.version, matches

    def search_discovery(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []
        if WORK_ID_RE.fullmatch(q):
            found = self.work(q)
            return [found["work"]] if found else []
        isbn13 = to_isbn13(q)
        if isbn13:
            ed = self.edition("isbn", isbn13)
            if ed:
                return [ed[1]]
        folded = norm(q)
        if not folded:
            return []
        like = f"%{folded}%"
        rows = self.conn.execute(
            "SELECT DISTINCT w.id FROM work w "
            "LEFT JOIN work_alias wa ON wa.work_id = w.id "
            "LEFT JOIN work_author wau ON wau.work_id = w.id "
            "LEFT JOIN author a ON a.id = wau.author_id "
            "LEFT JOIN author_alias aa ON aa.author_id = a.id "
            "WHERE fold(w.title) LIKE ? OR fold(wa.title) LIKE ? "
            "OR fold(a.name) LIKE ? OR fold(aa.name) LIKE ? "
            "ORDER BY fold(w.title) = ? DESC, w.title LIMIT ?",
            (like, like, like, like, folded, max(1, min(int(limit), 25))),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            w = self._work_view(r["id"])
            if w:
                out.append(w)
        return out

    def verified_cover(self, work_id: str, expected_sha256: str | None = None) -> tuple[bytes, str] | None:
        if self.http_client is None:
            return None
        return self.http_client.verified_cover(work_id, expected_sha256=expected_sha256)


class Lookups:
    """Unified lookup adapter for ``matcher.classify``, ``apply``, ``source``, and ``dialog``."""

    def __init__(self, backend: HttpCatalogClient | SnapshotCatalogClient) -> None:
        self.backend = backend
        self._work_cache: dict[str, dict[str, Any] | None] = {}
        self._isbn_cache: dict[str, dict[str, Any] | None] = {}

    @property
    def version(self) -> str | None:
        return getattr(self.backend, "version", None)

    def clear_cache(self) -> None:
        self._work_cache.clear()
        self._isbn_cache.clear()

    def begin(self) -> None:
        if hasattr(self.backend, "begin"):
            self.backend.begin()

    def work(self, ident: str) -> dict[str, Any] | None:
        if ident in self._work_cache:
            return self._work_cache[ident]
        found = self.backend.work(ident)
        work = found["work"] if found else None
        self._work_cache[ident] = work
        if work and work.get("id"):
            self._work_cache[work["id"]] = work
        return work

    def edition(self, isbn: str) -> dict[str, Any] | None:
        key = to_isbn13(isbn) or isbn
        if key in self._isbn_cache:
            return self._isbn_cache[key]
        found = self.backend.edition("isbn", key)
        work = found[1] if found else None
        self._isbn_cache[key] = work
        if work and work.get("id") and "isbn_disputed" not in (work.get("flags") or []):
            self._work_cache.setdefault(work["id"], work)
        return work

    def works(self, title: str, authors: list[str] | tuple[str, ...] = ()) -> list[dict[str, Any]]:
        if not title or not str(title).strip():
            return []
        found = self.backend.works(str(title).strip(), authors)[1]
        for w in found:
            if isinstance(w, dict) and w.get("id"):
                self._work_cache.setdefault(w["id"], w)
        return found

    def search_discovery(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        if hasattr(self.backend, "search_discovery"):
            results = self.backend.search_discovery(query, limit=limit)
            for w in results:
                if isinstance(w, dict) and w.get("id"):
                    self._work_cache.setdefault(w["id"], w)
            return results
        return []

    def verified_cover(self, work_id: str, expected_sha256: str | None = None) -> tuple[bytes, str] | None:
        if hasattr(self.backend, "verified_cover"):
            return self.backend.verified_cover(work_id, expected_sha256=expected_sha256)
        return None

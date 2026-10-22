"""Persistent, disposable library match results; never writes Calibre metadata."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

# Bump when classification semantics or serialized scan rows change.
MATCHER_REVISION = 1


def fingerprint(book: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(book, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def default_cache_path() -> Path | None:
    try:
        from calibre.constants import config_dir
    except ImportError:
        return None
    return Path(config_dir) / "plugins" / "bl_catalog" / "library-scans.sqlite"


class ScanCache:
    def __init__(self, path: str | Path, library: str) -> None:
        self.path = Path(path)
        self.library = library
        self.error = ""

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=2)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS scans (library TEXT, book_id INTEGER, "
                         "fingerprint TEXT, scope TEXT, version TEXT, row_json TEXT, scanned_at TEXT, "
                         "PRIMARY KEY (library, book_id))")
        except BaseException:
            conn.close()
            raise
        return conn

    def load(self) -> dict[int, dict[str, Any]]:
        conn = None
        try:
            conn = self._connect()
            records = conn.execute("SELECT book_id, fingerprint, scope, version, row_json, scanned_at "
                                   "FROM scans WHERE library=? ORDER BY book_id", (self.library,)).fetchall()
            result = {}
            for bid, fp, scope, version, raw, scanned_at in records:
                row = json.loads(raw)
                if not isinstance(row, dict) or row.get("id") != bid or "bucket" not in row:
                    raise ValueError("Invalid cached scan row")
                result[bid] = {"fingerprint": fp, "scope": scope, "version": version,
                               "row": row, "scanned_at": scanned_at}
            self.error = ""
            return result
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            self.error = str(exc)
            return {}
        finally:
            if conn is not None:
                conn.close()

    def save(self, books, rows, scope: str, version: str, live_ids: set[int]) -> None:
        conn = None
        try:
            conn = self._connect()
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            with conn:
                existing = conn.execute("SELECT book_id FROM scans WHERE library=?", (self.library,))
                removed = [(self.library, bid) for (bid,) in existing if bid not in live_ids]
                conn.executemany("DELETE FROM scans WHERE library=? AND book_id=?", removed)
                for row in rows:
                    bid = int(row["id"])
                    # Transient catalogue failures must be retried on the next delta scan.
                    if row["bucket"] == "error":
                        conn.execute("DELETE FROM scans WHERE library=? AND book_id=?", (self.library, bid))
                        continue
                    conn.execute("INSERT OR REPLACE INTO scans VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (self.library, bid, fingerprint(books[bid]), scope, version,
                                  json.dumps(row, ensure_ascii=False), now))
            self.error = ""
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            self.error = str(exc)
        finally:
            if conn is not None:
                conn.close()

"""Portable Calibre plugin fixtures with no catalogue-service or build dependencies."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from bl_catalog_calibre.matcher import CatalogueError, title_of

GOOD_ISBN = "9781784967123"

_SNAPSHOT_SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE work (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    sort_title      TEXT,
    type            TEXT,
    setting         TEXT,
    synopsis        TEXT,
    word_count      INTEGER,
    length_class    TEXT,
    first_published TEXT
);

CREATE TABLE edition (
    id              TEXT PRIMARY KEY,
    work_id         TEXT REFERENCES work (id),
    format          TEXT,
    isbn10          TEXT,
    isbn13          TEXT,
    asin            TEXT,
    audible_asin    TEXT,
    publisher       TEXT,
    imprint         TEXT,
    release_date    TEXT,
    region          TEXT,
    pages           INTEGER,
    runtime_minutes INTEGER,
    cover_artist    TEXT,
    cover_url       TEXT,
    price           REAL,
    price_currency  TEXT,
    in_print        INTEGER
);

CREATE TABLE work_alias (
    work_id TEXT NOT NULL REFERENCES work (id),
    title   TEXT NOT NULL,
    PRIMARY KEY (work_id, title)
);

CREATE TABLE author (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    sort_name TEXT
);

CREATE TABLE author_alias (
    author_id INTEGER NOT NULL REFERENCES author (id),
    name      TEXT NOT NULL,
    PRIMARY KEY (author_id, name)
);

CREATE TABLE work_author (
    work_id   TEXT NOT NULL REFERENCES work (id),
    author_id INTEGER NOT NULL REFERENCES author (id),
    role      TEXT NOT NULL CHECK (role IN ('author', 'editor', 'narrator', 'cover_artist')),
    PRIMARY KEY (work_id, author_id, role)
);

CREATE TABLE series (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE work_series (
    work_id   TEXT NOT NULL REFERENCES work (id),
    series_id INTEGER NOT NULL REFERENCES series (id),
    position  REAL,
    PRIMARY KEY (work_id, series_id)
);

CREATE TABLE work_tag (
    work_id TEXT NOT NULL REFERENCES work (id),
    kind    TEXT NOT NULL CHECK (kind IN ('faction', 'character', 'planet')),
    value   TEXT NOT NULL,
    PRIMARY KEY (work_id, kind, value)
);

CREATE TABLE work_ident (
    work_id TEXT NOT NULL REFERENCES work (id),
    kind    TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (work_id, kind, value)
);

CREATE TABLE edition_ident (
    edition_id TEXT NOT NULL REFERENCES edition (id),
    kind       TEXT NOT NULL,
    value      TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'disputed')),
    PRIMARY KEY (edition_id, kind, value)
);

CREATE TABLE field_support (
    entity        TEXT NOT NULL CHECK (entity IN ('work', 'edition')),
    entity_id     TEXT NOT NULL,
    field         TEXT NOT NULL,
    value         TEXT,
    score         REAL NOT NULL,
    agree_json    TEXT NOT NULL,
    disagree_json TEXT NOT NULL
);

CREATE TABLE id_alias (
    old_id TEXT PRIMARY KEY,
    new_id TEXT NOT NULL
);

CREATE TABLE work_cover (
    work_id    TEXT PRIMARY KEY REFERENCES work (id),
    source_url TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    width      INTEGER NOT NULL CHECK (width > 0),
    height     INTEGER NOT NULL CHECK (height > 0),
    media_type TEXT NOT NULL
);

CREATE TABLE work_contains (
    parent_id TEXT NOT NULL REFERENCES work (id),
    child_id  TEXT NOT NULL REFERENCES work (id),
    position  INTEGER,
    PRIMARY KEY (parent_id, child_id)
);
"""


def make_work(
    ident: str,
    title: str,
    authors: list[str],
    isbn: str | None = GOOD_ISBN,
    *,
    work_type: str = "novel",
    setting: str = "Warhammer 40,000",
    synopsis: str | None = "A Horus Heresy novel.",
    series: list[dict] | None = None,
    contents: list[dict] | None = None,
    cover: dict | None = None,
    flags: list[str] | None = None,
) -> dict:
    editions = []
    if isbn is not None:
        editions.append({
            "id": "e_1",
            "work_id": ident,
            "format": {"value": "ebook"},
            "isbn13": {"value": isbn},
            "isbn10": None,
            "publisher": {"value": "Black Library"},
            "release_date": {"value": "2006-04-01"},
            "flags": [],
            "identifiers": [{"kind": "isbn13", "value": isbn, "status": "ok"}],
        })
    return {
        "id": ident,
        "title": {"value": title, "score": 1.0, "agree": ["lexicanum", "openlibrary"], "disagree": []},
        "type": {"value": work_type, "score": 1.0, "agree": ["lexicanum"], "disagree": []},
        "setting": {"value": setting, "score": 1.0, "agree": ["lexicanum"], "disagree": []},
        "synopsis": {"value": synopsis, "score": 0.9, "agree": ["lexicanum"], "disagree": []} if synopsis else None,
        "first_published": {"value": "2006-04-01", "score": 1.0, "agree": ["lexicanum"], "disagree": []},
        "authors": [
            {"id": i + 1, "name": a, "sort_name": a, "role": "author", "score": 1.0, "aliases": []}
            for i, a in enumerate(authors)
        ],
        "series": series if series is not None else [{"id": 1, "name": "The Horus Heresy", "position": 1}],
        "aliases": [],
        "tags": [{"kind": "faction", "value": "Sons of Horus"}],
        "identifiers": [],
        "editions": editions,
        "contents": contents or [],
        "cover": cover,
        "flags": flags or [],
    }


def make_book(
    title: str = "Horus Rising",
    authors: tuple[str, ...] | list[str] = ("Dan Abnett",),
    *,
    bid: int = 1,
    **identifiers: str,
) -> dict:
    return {
        "id": bid,
        "title": title,
        "authors": list(authors),
        "identifiers": dict(identifiers),
        "formats": ["EPUB"],
        "series": "",
        "series_index": None,
        "comments": "",
        "tags": [],
    }


class FakeLookups:
    def __init__(
        self,
        works: list[dict] = (),
        isbns: dict[str, str] | None = None,
        *,
        fail: str | None = None,
        covers: dict[str, bytes] | None = None,
    ) -> None:
        self.by_id = {w["id"]: w for w in works}
        self.isbns = isbns or {}
        self.fail = fail
        self.covers = covers or {}
        self.version = "2026.10.21.1"

    def begin(self) -> None:
        pass

    def _check(self) -> None:
        if self.fail:
            raise CatalogueError(self.fail)

    def work(self, ident: str) -> dict | None:
        self._check()
        return self.by_id.get(ident)

    def edition(self, isbn: str) -> dict | None:
        self._check()
        wid = self.isbns.get(isbn)
        return self.by_id.get(wid) if wid else None

    def works(self, title: str, authors: list[str] | tuple[str, ...] = ()) -> list[dict]:
        self._check()
        return list(self.by_id.values())

    def search_discovery(self, query: str, limit: int = 10) -> list[dict]:
        self._check()
        q = query.strip().lower()
        return [
            w
            for w in self.by_id.values()
            if q in w["id"].lower() or q in title_of(w).lower()
        ][:limit]

    def verified_cover(self, work_id: str, expected_sha256: str | None = None) -> tuple[bytes, str] | None:
        self._check()
        raw = self.covers.get(work_id)
        if not raw:
            return None
        if expected_sha256 and hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise CatalogueError("Cover SHA-256 mismatch")
        return raw, "image/jpeg"


class FakeWriter:
    """PR #208 compatible FakeWriter + support for optional metadata fields."""

    def __init__(self, books: list[dict], *, ready: bool = True, drift: bool = False) -> None:
        self.books = [json.loads(json.dumps(b)) for b in books]
        self.states = {
            b["id"]: {
                "title": b["title"],
                "authors": list(b["authors"]),
                "series": b.get("series", ""),
                "series_index": b.get("series_index"),
                "comments": b.get("comments", ""),
                "tags": list(b.get("tags", ())),
                "identifiers": [[k, v] for k, v in b["identifiers"].items()],
            }
            for b in books
        }
        self.ready = ready
        self.drift = drift
        self.writes: list[tuple[int, dict]] = []
        self.covers_written: dict[int, bytes] = {}

    def available(self) -> bool:
        return self.ready

    def read_books(self, book_ids=None) -> list[dict]:
        if book_ids is None:
            return [json.loads(json.dumps(b)) for b in self.books]
        wanted = set(book_ids)
        return [json.loads(json.dumps(b)) for b in self.books if b["id"] in wanted]

    def state(self, bid: int) -> dict:
        return {"state": json.loads(json.dumps(self.states[bid]))}

    def set_fields(self, bid: int, changes: dict, expected=None, cover_bytes: bytes | None = None) -> dict:
        self.writes.append((bid, changes))
        if cover_bytes is not None:
            self.covers_written[bid] = cover_bytes
        if not self.drift:
            for k, v in changes.items():
                if k == "identifiers":
                    self.states[bid]["identifiers"] = [[ik, iv] for ik, iv in v.items()]
                else:
                    self.states[bid][k] = v
        return {
            "ok": True,
            "history_id": f"h{bid}",
            "recovery": f"/recovery/h{bid}",
            "after": self.state(bid)["state"],
        }


def build_snapshot_db(path: Path) -> None:
        schema = _SNAPSHOT_SCHEMA
        conn = sqlite3.connect(path)
        conn.executescript(schema)
        conn.execute("INSERT INTO meta (key, value) VALUES ('version', '2026.10.21.5')")
        conn.execute(
            "INSERT INTO work (id, title, sort_title, type, setting, synopsis, first_published) "
            "VALUES ('w_horus', 'Horus Rising', 'Horus Rising', 'novel', 'Warhammer 40,000', 'Warmaster.', '2006-04-01')"
        )
        conn.execute("INSERT INTO id_alias (old_id, new_id) VALUES ('w_old_horus', 'w_horus')")
        conn.execute("INSERT INTO author (id, name, sort_name) VALUES (1, 'Dan Abnett', 'Abnett, Dan')")
        conn.execute("INSERT INTO author_alias (author_id, name) VALUES (1, 'D. Abnett')")
        conn.execute("INSERT INTO work_author (work_id, author_id, role) VALUES ('w_horus', 1, 'author')")
        conn.execute(
            "INSERT INTO field_support (entity, entity_id, field, value, score, agree_json, disagree_json) "
            "VALUES ('work', 'w_horus', 'title', 'Horus Rising', 1.0, '[\"lexicanum\",\"openlibrary\"]', '[]'), "
            "       ('work', 'w_horus', 'author', 'Dan Abnett', 1.0, '[\"lexicanum\"]', '[]')"
        )
        conn.execute("INSERT INTO series (id, name) VALUES (10, 'The Horus Heresy')")
        conn.execute("INSERT INTO work_series (work_id, series_id, position) VALUES ('w_horus', 10, 1.0)")
        conn.execute(
            "INSERT INTO edition (id, work_id, format, isbn13, publisher, release_date) "
            f"VALUES ('e_1', 'w_horus', 'ebook', '{GOOD_ISBN}', 'Black Library', '2006-04-01')"
        )
        conn.execute(
            "INSERT INTO edition_ident (edition_id, kind, value, status) "
            f"VALUES ('e_1', 'isbn13', '{GOOD_ISBN}', 'ok')"
        )
        conn.execute(
            "INSERT INTO work_cover (work_id, source_url, sha256, width, height, media_type) "
            "VALUES ('w_horus', 'https://wh40k.lexicanum.com/wiki/Special:FilePath/Horus.jpg', "
            f"'{'a' * 64}', 1200, 1800, 'image/jpeg')"
        )
        conn.commit()
        conn.close()

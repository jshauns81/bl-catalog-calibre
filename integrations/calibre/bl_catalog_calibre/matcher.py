"""BL-Catalog matching, classification, normalization, and metadata extraction.

Implements the exact classification buckets and rules from library-finish PR #208 (D078)
and anthology story-credit rules from PR #205 (D077), using only the Python standard
library so it runs unchanged inside Calibre's embedded Python or standalone tests.
"""

from __future__ import annotations

import collections
import csv
import re
import unicodedata
from pathlib import Path
from typing import Any

from .config import ApplyOptions

BUCKETS = (
    "already",
    "certain_isbn",
    "certain_title_author",
    "picked",
    "ambiguous",
    "conflict",
    "not_in_blc",
    "error",
)
CERTAIN = ("certain_isbn", "certain_title_author")
APPLICABLE_BUCKETS = ("certain_isbn", "certain_title_author", "picked")

WORK_ID_RE = re.compile(r"w_[A-Za-z0-9_]{1,80}")
TRAILING_YEAR_RE = re.compile(r"\s*\((?:19|20)\d{2}\)\s*$")
ARTICLE_SUFFIX_RE = re.compile(r"^(.+?),\s*(the|a|an)\s*$", flags=re.IGNORECASE)

AUDIO_FORMATS = frozenset({"audiobook", "audio", "audio_drama", "cd", "mp3"})
COLLECTION_TYPES = frozenset({"anthology", "collection", "omnibus"})
MAX_OFFERED_AUTHORS = 20
GOOD_COVER_WIDTH = 1000

CSV_FIELDS = [
    "id",
    "title",
    "authors",
    "formats",
    "isbn",
    "bl_catalog",
    "bucket",
    "work_id",
    "blc_title",
    "blc_authors",
    "add_bl_catalog",
    "add_isbn",
    "reason",
]


class CatalogueError(ValueError):
    """Raised when BL-Catalog is unreachable or returns an invalid payload."""


def norm(value: Any) -> str:
    """Fold accents to ASCII and collapse non-alphanumeric runs to single spaces."""
    text = (
        unicodedata.normalize("NFKD", str(value or ""))
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def calibre_title_norm(value: Any) -> str:
    """Normalised title where Calibre's 'DARKLING HOURS, THE' reads as 'the darkling hours'."""
    raw = str(value or "").strip()
    inverted = ARTICLE_SUFFIX_RE.sub(r"\2 \1", raw)
    return norm(inverted)


def initials_joined(value: Any) -> str:
    """Normalised author name where runs of initials join ('C. L. Werner' -> 'cl. werner').

    Joined initials keep a trailing dot so 'Al Ewing' ('al ewing') never equals 'A. L. Ewing' ('al. ewing').
    """
    text = (
        unicodedata.normalize("NFKD", str(value or ""))
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    mixed = text != text.lower() and text != text.upper()
    words: list[str] = []
    run = ""
    for token in re.findall(r"[A-Za-z0-9]+", text):
        if token.isalpha() and (
            (len(token) == 1 and (token.isupper() or not mixed))
            or (mixed and 2 <= len(token) <= 3 and token.isupper())
        ):
            run += token.lower()
            continue
        if run:
            words.append(run + ".")
            run = ""
        words.append(token.lower())
    if run:
        words.append(run + ".")
    return " ".join(words)


def author(value: Any) -> str:
    """Normalized author key."""
    return initials_joined(value)


PLACEHOLDER_AUTHORS = frozenset({
    norm(name)
    for name in (
        "unknown",
        "various",
        "various authors",
        "black library",
        "games workshop",
        "none",
        "anonymous",
        "tba",
        "tbd",
    )
})

UMBRELLA_SERIES = frozenset({
    norm(name)
    for name in (
        "Warhammer 40,000",
        "Warhammer 40k",
        "Warhammer",
        "Warhammer Fantasy",
        "Warhammer Fantasy Battle",
        "Warhammer Age of Sigmar",
        "Age of Sigmar",
        "Warhammer: The Old World",
        "The Old World",
    )
})


def scalar_value(field: Any) -> Any:
    """Extract ``field['value']`` from a BLC field-support wrapper, or return ``field``."""
    return field.get("value") if isinstance(field, dict) else field


def plain_title(field: Any) -> str:
    """Return title text with any trailing '(YYYY)' disambiguator removed."""
    val = scalar_value(field)
    if not isinstance(val, str):
        return ""
    return TRAILING_YEAR_RE.sub("", val).strip()


def title_of(work: dict[str, Any] | None) -> str:
    if not isinstance(work, dict):
        return ""
    val = scalar_value(work.get("title"))
    return val.strip() if isinstance(val, str) else ""


def _valid_isbn10(digits: str) -> bool:
    if len(digits) != 10 or not digits[:9].isdigit() or (not digits[9].isdigit() and digits[9] != "X"):
        return False
    total = sum((10 - i) * (10 if ch == "X" else int(ch)) for i, ch in enumerate(digits))
    return total % 11 == 0


def _valid_isbn13(digits: str) -> bool:
    if len(digits) != 13 or not digits.isdigit() or not digits.startswith(("978", "979")):
        return False
    total = sum((1 if i % 2 == 0 else 3) * int(ch) for i, ch in enumerate(digits[:12]))
    check = (10 - (total % 10)) % 10
    return check == int(digits[12])


def to_isbn13(raw: Any) -> str | None:
    """Validate an ISBN-10 or ISBN-13 and return its canonical 13-digit ISBN-13, or None."""
    if not isinstance(raw, str):
        return None
    cleaned = re.sub(r"[\s-]+", "", raw.strip()).upper()
    if _valid_isbn13(cleaned):
        return cleaned
    if _valid_isbn10(cleaned):
        stem = "978" + cleaned[:9]
        total = sum((1 if i % 2 == 0 else 3) * int(ch) for i, ch in enumerate(stem))
        check = (10 - (total % 10)) % 10
        return f"{stem}{check}"
    return None


def valid_isbn(raw: Any) -> str | None:
    """Return normalized valid ISBN (13 or 10 digits) when checksum is valid, else None."""
    if not isinstance(raw, str):
        return None
    cleaned = re.sub(r"[\s-]+", "", raw.strip()).upper()
    if _valid_isbn13(cleaned) or _valid_isbn10(cleaned):
        return cleaned
    return None


def isbns_of(book: dict[str, Any]) -> list[str]:
    """Extract deduplicated valid ISBNs from a Calibre book dict."""
    identifiers = book.get("identifiers") or {}
    raw_values = []
    for k, v in identifiers.items():
        if str(k).lower() in ("isbn", "isbn13", "isbn10") and v:
            raw_values.append(str(v))
    found: list[str] = []
    for raw in raw_values:
        for part in re.split(r"[,;\s]+", raw):
            val = valid_isbn(part)
            if val and val not in found:
                found.append(val)
    return found


def _author_credits(work: dict[str, Any], plain_names: bool = False) -> list[dict[str, Any]]:
    """Confirmed author credits (role == 'author' and score == 1)."""
    people = work.get("authors")
    if not isinstance(people, list):
        return []
    normalized = [
        {"name": p, "role": "author", "score": 1}
        if plain_names and isinstance(p, str)
        else p
        for p in people
    ]
    return [
        p
        for p in normalized
        if isinstance(p, dict)
        and isinstance(p.get("name"), str)
        and p["name"].strip()
        and p.get("role") == "author"
        and type(p.get("score")) in (int, float)
        and p["score"] == 1
    ]


def authors_of(work: dict[str, Any], plain_names: bool = False) -> list[str]:
    """Return confirmed author names for a work."""
    return [p["name"].strip() for p in _author_credits(work, plain_names)]


def author_aliases(work: dict[str, Any]) -> list[dict[str, Any]]:
    """Return [{'canonical': name, 'spellings': [...]}] for each confirmed author on ``work``."""
    groups: list[dict[str, Any]] = []
    for p in _author_credits(work):
        name = p["name"].strip()
        aliases = p.get("aliases", [])
        if (
            not isinstance(aliases, list)
            or len(aliases) > 40
            or any(not isinstance(a, str) or not (1 <= len(a.strip()) <= 300) for a in aliases)
        ):
            aliases = []
        spellings = list(dict.fromkeys([name] + [a.strip() for a in aliases]))
        group: dict[str, Any] = {"canonical": name, "spellings": spellings}
        if type(p.get("id")) is int and p["id"] > 0:
            group["person_id"] = p["id"]
        groups.append(group)
    return groups


def author_relation(authors: list[str] | tuple[str, ...], work: dict[str, Any]) -> str:
    """Compare book authors to ``work``'s confirmed credits: 'same', 'partial', 'none', or 'unknown'."""
    groups = [{author(s) for s in g["spellings"]} for g in author_aliases(work)]
    if not groups:
        return "unknown"
    hit: set[int] = set()
    exact = bool(authors)
    for name in authors:
        matches = [i for i, g in enumerate(groups) if author(name) in g]
        if len(matches) == 1:
            hit.add(matches[0])
        else:
            exact = False
            hit.update(matches)
    if exact and hit == set(range(len(groups))):
        return "same"
    return "partial" if hit else "none"


def offered_credits(work: dict[str, Any]) -> tuple[list[str], str | None]:
    """Return ``(authors, credits_kind)`` offered for manual work selection (D077).

    For anthologies/collections/omnibuses where every child in ``contents`` credits its story
    authors, returns ``(story_authors + confirmed_authors, 'contents')``.
    """
    def _real(names):
        return [n for n in dict.fromkeys(names) if norm(n) not in PLACEHOLDER_AUTHORS]

    confirmed = _real(authors_of(work))
    stories: list[str] = []
    contents = work.get("contents")
    if scalar_value(work.get("type")) in COLLECTION_TYPES and isinstance(contents, list) and contents:
        named = [
            authors_of(item, plain_names=True) if isinstance(item, dict) else []
            for item in contents
        ]
        if all(named):
            stories = _real(name for names in named for name in names)
    credits: str | None = "contents" if stories else "confirmed"
    if not confirmed and not stories:
        people = work.get("authors") if isinstance(work.get("authors"), list) else []
        weak = [
            p
            for p in people
            if isinstance(p, dict)
            and isinstance(p.get("name"), str)
            and p["name"].strip()
            and p.get("role") == "author"
            and type(p.get("score")) in (int, float)
            and p["score"] > 0
        ]
        weak.sort(key=lambda p: -p["score"])
        confirmed = _real(p["name"].strip() for p in weak)
        credits = "unconfirmed"
    names: list[str] = []
    seen: set[str] = set()
    for name in stories + confirmed:
        key = author(name)
        if key not in seen:
            seen.add(key)
            names.append(name)
    return (names[:MAX_OFFERED_AUTHORS], credits) if names else ([], None)


def picked_contents_match(work: dict[str, Any], title: str, authors: list[str] | tuple[str, ...]) -> bool:
    """True when an anthology work's offered story credits match ``authors`` (or when picked directly)."""
    if not isinstance(title, str) or calibre_title_norm(plain_title(work.get("title"))) != calibre_title_norm(plain_title(title)):
        return False
    offered, credits = offered_credits(work)
    if credits != "contents":
        return False
    book_set = {author(n) for n in authors if author(n)}
    offered_set = {author(n) for n in offered if author(n)}
    return bool(book_set) and book_set <= offered_set


def choose_isbn(work: dict[str, Any], validator=valid_isbn) -> dict[str, Any] | None:
    """Select the work's best text ISBN: ebook first, unflagged before flagged, never isbn_disputed."""
    ranked: list[tuple[tuple[bool, bool, int], dict[str, Any]]] = []
    for order, edition in enumerate(work.get("editions") or []):
        if not isinstance(edition, dict):
            continue
        flags = {f for f in (edition.get("flags") or []) if isinstance(f, str)}
        if "isbn_disputed" in flags:
            continue
        kind = scalar_value(edition.get("format"))
        kind = kind.strip().lower() if isinstance(kind, str) else None
        if kind in AUDIO_FORMATS:
            continue
        numbers = (scalar_value(edition.get(f)) for f in ("isbn13", "isbn10"))
        value = next(
            (v for v in (validator(n) if isinstance(n, str) else None for n in numbers) if v),
            None,
        )
        if value:
            ranked.append((
                (kind != "ebook", bool(flags), order),
                {"value": value, "format": kind, "edition_id": edition.get("id")},
            ))
    return min(ranked, key=lambda r: r[0])[1] if ranked else None


def series_of(work: dict[str, Any]) -> dict[str, Any] | None:
    """Return ``{'series': name, 'series_index': pos}`` for the primary non-umbrella series, or None."""
    raw = work.get("series")
    items = raw if isinstance(raw, list) else [raw] if raw else []
    setting = norm(scalar_value(work.get("setting")))
    found: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = scalar_value(item.get("name") or item.get("title") or item.get("value"))
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        if "reading order" in name.casefold():
            continue
        if norm(name) in UMBRELLA_SERIES | ({setting} if setting else set()):
            continue
        pos = scalar_value(item.get("position") if "position" in item else item.get("series_index"))
        idx: float | int | None = None
        if isinstance(pos, (int, float)) and not isinstance(pos, bool) and pos > 0:
            idx = int(pos) if float(pos).is_integer() else float(pos)
        found.append({"series": name, "series_index": idx})
    if not found:
        return None
    if len(found) > 1:
        numbered = [f for f in found if f["series_index"] is not None]
        if numbered:
            found = numbered
    return found[0]


def tags_of(work: dict[str, Any]) -> list[str]:
    """Return clean tags from work setting, type, and work_tag entries."""
    out: list[str] = []
    setting = scalar_value(work.get("setting"))
    if isinstance(setting, str) and setting.strip():
        out.append(setting.strip())
    w_type = scalar_value(work.get("type"))
    if isinstance(w_type, str) and w_type.strip():
        out.append(w_type.strip().replace("_", " ").title())
    for t in work.get("tags") or []:
        if isinstance(t, dict) and isinstance(t.get("value"), str) and t["value"].strip():
            val = t["value"].strip()
            if val not in out:
                out.append(val)
    return out


def cover_of(work: dict[str, Any]) -> dict[str, Any] | None:
    """Extract validated cover metadata dict from ``work`` if available."""
    cov = work.get("cover")
    if not isinstance(cov, dict):
        return None
    url = cov.get("url")
    sha256 = cov.get("sha256")
    width = cov.get("width")
    height = cov.get("height")
    if (
        isinstance(url, str)
        and url.startswith("/v1/work/")
        and isinstance(sha256, str)
        and len(sha256) == 64
        and isinstance(width, int)
        and width > 0
        and isinstance(height, int)
        and height > 0
    ):
        upscaled = bool(cov.get("upscaled"))
        return {
            "url": url,
            "sha256": sha256,
            "width": width,
            "height": height,
            "upscaled": upscaled,
            "source": cov.get("source") or "lexicanum",
            "source_url": cov.get("source_url") or "",
            "label": f"Lexicanum cover{' \u00b7 Upscaled' if upscaled else ''} \u00b7 {width}\u00d7{height}",
            "good": upscaled or width >= GOOD_COVER_WIDTH,
        }
    return None


def _additions(book: dict[str, Any], work: dict[str, Any], with_id: bool = True) -> dict[str, str]:
    add: dict[str, str] = {"bl_catalog": work["id"]} if with_id else {}
    if not isbns_of(book):
        choice = choose_isbn(work, valid_isbn)
        if choice:
            add["isbn"] = choice["value"]
    return add


def _row(
    bucket: str,
    reason: str,
    work: dict[str, Any] | None = None,
    add: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "bucket": bucket,
        "reason": reason,
        "add": add or {},
        "work_id": work["id"] if work else "",
        "blc_title": title_of(work) if work else "",
        "blc_authors": "; ".join(authors_of(work)) if work else "",
        "work": work,
    }


def classify(book: dict[str, Any], lookups: Any) -> dict[str, Any]:
    """Classify one Calibre book into a PR #208 bucket using ``lookups``."""
    try:
        return _classify(book, lookups)
    except CatalogueError as error:
        return _row("error", str(error))


def _classify(book: dict[str, Any], lookups: Any) -> dict[str, Any]:
    note = ""
    identifiers = book.get("identifiers") or {}
    stamped = identifiers.get("bl_catalog")
    if stamped:
        found = lookups.work(stamped) if WORK_ID_RE.fullmatch(str(stamped)) else None
        if found:
            with_id = found["id"] != stamped  # update if merged in BLC
            reason = (
                f"BLC merged {stamped} -> {found['id']}"
                if with_id
                else f"BLC confirms {stamped}"
            )
            return _row("already", reason, found, _additions(book, found, with_id=with_id))
        note = f"stale bl_catalog {stamped} not in BLC; "

    hits: dict[str, tuple[str, dict[str, Any]]] = {}
    for isbn in isbns_of(book):
        edition_work = lookups.edition(isbn)
        if edition_work:
            hits.setdefault(edition_work["id"], (isbn, edition_work))
    if len(hits) > 1:
        return _row("ambiguous", note + "ISBNs name different works: " + ", ".join(sorted(hits)))
    if hits:
        (isbn, edition_work), = hits.values()
        if "isbn_disputed" in (edition_work.get("flags") or []):
            return _row("ambiguous", note + f"ISBN {isbn} is disputed in BLC", edition_work)
        work = lookups.work(edition_work["id"]) or edition_work
        want = calibre_title_norm(plain_title(book.get("title")))
        if want and want == calibre_title_norm(plain_title(work.get("title"))):
            relation = author_relation(book.get("authors") or (), work)
            return _row(
                "certain_isbn",
                f"{note}ISBN {isbn}; title agrees; authors {relation}",
                work,
                _additions(book, work),
            )
        return _row("conflict", f'{note}ISBN {isbn} is the work "{title_of(work)}"', work)

    book_title = str(book.get("title") or "")
    book_authors = list(book.get("authors") or ())
    candidates = [
        w
        for w in lookups.works(book_title, book_authors)
        if calibre_title_norm(title_of(w)) == calibre_title_norm(book_title)
    ]
    if not candidates:
        return _row("not_in_blc", note + "no BLC work with this full title")
    relations = [(w, author_relation(book_authors, w)) for w in candidates]
    same = [w for w, r in relations if r == "same"]
    if len(same) == 1:
        return _row(
            "certain_title_author",
            note + "one work, same title and author set",
            same[0],
            _additions(book, same[0]),
        )
    if len(same) > 1:
        return _row(
            "ambiguous",
            note + "several works share title and authors: " + ", ".join(w["id"] for w in same),
        )
    partial = [w for w, r in relations if r == "partial"]
    if partial:
        return _row(
            "ambiguous",
            note + "partial author overlap with " + ", ".join(w["id"] for w in partial),
            partial[0],
        )
    unknown = [w for w, r in relations if r == "unknown"]
    if unknown:
        return _row(
            "ambiguous",
            note + "BLC credits no confirmed author for " + ", ".join(w["id"] for w in unknown),
            unknown[0],
        )
    return _row("not_in_blc", note + "title exists only under other authors")


def demote_shared(rows: list[dict[str, Any]]) -> None:
    """Two Calibre books proposing the same work (or one already carrying it) are demoted to ambiguous."""
    owners: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        if row.get("work_id") and row.get("bucket") in ("already", "picked") + CERTAIN:
            owners[row["work_id"]].append(row)
    for work_id, claimants in owners.items():
        if len(claimants) > 1:
            ids = ", ".join(str(r["id"]) for r in claimants)
            for row in claimants:
                if row["bucket"] in CERTAIN or row["bucket"] == "picked":
                    row.update(
                        bucket="ambiguous",
                        add={},
                        proposals={},
                        reason=f"{row['reason']}; work {work_id} also claimed by Calibre books {ids}",
                    )


def build_proposals(
    book: dict[str, Any],
    work: dict[str, Any] | None,
    options: ApplyOptions,
    *,
    picked: bool = False,
    with_id: bool = True,
) -> dict[str, Any]:
    """Compute field updates for ``book`` from ``work`` according to ``options``.

    Respects D077/D078:
    - Existing valid ISBNs are never replaced.
    - For a manually picked work, ISBN and cover are lent only when authors are confirmed
      or come from complete anthology story contents (``offered_credits``), so a merged
      record's other books never lend their editions.
    """
    if not work:
        return {}
    proposals: dict[str, Any] = {}
    book_title = str(book.get("title") or "")
    book_authors = list(book.get("authors") or ())
    rel = author_relation(book_authors, work)
    offered, credits_kind = offered_credits(work)
    authors_trusted = rel == "same" or (picked and credits_kind in ("confirmed", "contents"))

    if options.identifiers:
        ident_add: dict[str, str] = {}
        if with_id:
            ident_add["bl_catalog"] = work["id"]
        if not isbns_of(book) and (not picked or authors_trusted):
            choice = choose_isbn(work, valid_isbn)
            if choice:
                ident_add["isbn"] = choice["value"]
        if ident_add:
            proposals["identifiers"] = ident_add

    if options.title:
        target_title = plain_title(work.get("title")) or title_of(work)
        if target_title and target_title != book_title:
            proposals["title"] = target_title

    if options.authors:
        target_authors = offered if (picked and offered) else authors_of(work)
        if target_authors and list(target_authors) != book_authors:
            proposals["authors"] = list(target_authors)

    if options.series:
        ser = series_of(work)
        if ser and ser.get("series") and (
            ser["series"] != book.get("series") or ser.get("series_index") != book.get("series_index")
        ):
            proposals["series"] = ser

    if options.synopsis:
        syn = scalar_value(work.get("synopsis"))
        if isinstance(syn, str) and syn.strip() and syn.strip() != (book.get("comments") or "").strip():
            proposals["synopsis"] = syn.strip()

    if options.tags:
        w_tags = tags_of(work)
        existing_tags = list(book.get("tags") or ())
        merged_tags = list(dict.fromkeys([*existing_tags, *w_tags]))
        if merged_tags != existing_tags:
            proposals["tags"] = merged_tags

    if options.cover and (not picked or authors_trusted):
        cov = cover_of(work)
        if cov and cov["good"]:
            proposals["cover"] = cov

    return proposals


def apply_manual_pick(
    row: dict[str, Any],
    book: dict[str, Any],
    work: dict[str, Any],
    options: ApplyOptions | None = None,
) -> dict[str, Any]:
    """Update a scan row when Shaun manually picks a BLC work (e.g. for an anthology or conflict)."""
    opts = options or ApplyOptions()
    offered, credits_kind = offered_credits(work)
    proposals = build_proposals(book, work, opts, picked=True)
    add = dict(proposals.get("identifiers") or {"bl_catalog": work["id"]})
    note = f"Owner picked {work['id']}"
    if credits_kind == "contents":
        note += f" (anthology contents: {len(offered)} authors)"
    elif credits_kind == "unconfirmed":
        note += " (unconfirmed credits: keeps own ISBN/cover)"
    row.update(
        bucket="picked",
        reason=note,
        work_id=work["id"],
        blc_title=title_of(work),
        blc_authors="; ".join(offered or authors_of(work)),
        add=add,
        proposals=proposals,
        work=work,
        picked=True,
    )
    return row


def run_scan(
    books: list[dict[str, Any]],
    lookups: Any,
    options: ApplyOptions | None = None,
    progress=None,
    *,
    deduplicate: bool = True,
) -> list[dict[str, Any]]:
    """Classify a list of Calibre books and demote shared works."""
    opts = options or ApplyOptions()
    rows: list[dict[str, Any]] = []
    total = len(books)
    for n, book in enumerate(books, 1):
        if hasattr(lookups, "begin"):
            lookups.begin()
        result = classify(book, lookups)
        work = result.get("work")
        proposals: dict[str, Any] = {}
        if result["bucket"] in CERTAIN and work:
            proposals = build_proposals(book, work, opts, picked=False)
            if "identifiers" in proposals:
                result["add"] = dict(proposals["identifiers"])
        elif result["bucket"] == "already" and work:
            with_id = bool(result["add"].get("bl_catalog"))
            proposals = build_proposals(book, work, opts, picked=False, with_id=with_id)
        row = {
            "id": book["id"],
            "title": book.get("title") or "",
            "authors": "; ".join(book.get("authors") or ()),
            "formats": ",".join(book.get("formats") or ()),
            "isbn": (book.get("identifiers") or {}).get("isbn", ""),
            "bl_catalog": (book.get("identifiers") or {}).get("bl_catalog", ""),
            "proposals": proposals,
            "picked": False,
            **result,
        }
        rows.append(row)
        if progress:
            progress(n, total, row)
    if deduplicate:
        demote_shared(rows)
    return rows


def write_csv(rows: list[dict[str, Any]], path: str | Path) -> None:
    """Write scan rows in the exact 13-column CSV format from library-finish PR #208."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            add = row.get("add") or {}
            writer.writerow({
                **row,
                "add_bl_catalog": add.get("bl_catalog", ""),
                "add_isbn": add.get("isbn", ""),
            })

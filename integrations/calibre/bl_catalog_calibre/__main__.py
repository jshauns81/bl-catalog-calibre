"""Command-line dry-run and snapshot sync interface for the BL-Catalog Calibre plugin.

Allows running the plugin package (or the built ``bl-catalog-calibre.zip`` directly!)
from a terminal to sync a local BLC snapshot or produce the PR #208 13-column CSV
dry-run report against a read-only Calibre ``metadata.db``:

    python3 integrations/calibre/dist/bl-catalog-calibre.zip --sync-snapshot
    python3 integrations/calibre/dist/bl-catalog-calibre.zip --metadata-db /path/to/metadata.db --limit 50
"""

from __future__ import annotations

import argparse
import collections
import os
import sys
import time
import types
from pathlib import Path

if not __package__:
    _pkg_dir = str(Path(__file__).resolve().parent)
    _pkg = types.ModuleType("bl_catalog_calibre")
    _pkg.__path__ = [_pkg_dir]  # type: ignore[attr-defined]
    _pkg.__package__ = "bl_catalog_calibre"
    sys.modules.setdefault("bl_catalog_calibre", _pkg)
    __package__ = "bl_catalog_calibre"
    __spec__ = None

from .client import (
    HttpCatalogClient,
    Lookups,
    SnapshotCatalogClient,
    read_calibre_books_readonly,
)
from .config import DEFAULT_SNAPSHOT_PATH, DEFAULT_TIMEOUT, DEFAULT_URL
from .matcher import BUCKETS, run_scan, write_csv

DEFAULT_DB = "/mnt/unraid/Books/CalibreLibrary/metadata.db"
DEFAULT_CSV = "/tmp/blc-dry-run.csv"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--metadata-db", default=DEFAULT_DB, help="Path to Calibre metadata.db (opened read-only)")
    parser.add_argument("--url", default=os.environ.get("BL_CATALOG_URL") or DEFAULT_URL, help="BL-Catalog base URL")
    parser.add_argument("--token", default=os.environ.get("BL_CATALOG_TOKEN") or "", help="Optional bearer token")
    parser.add_argument(
        "--snapshot",
        default=DEFAULT_SNAPSHOT_PATH,
        help="Path to local SQLite snapshot file",
    )
    parser.add_argument(
        "--sync-snapshot",
        action="store_true",
        help="Download /v1/snapshots/latest.sqlite to --snapshot before running",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use live HTTP /v1/* lookups instead of a local SQLite snapshot",
    )
    parser.add_argument("--limit", type=int, help="Sample N books spread evenly across the library")
    parser.add_argument("--csv", default=DEFAULT_CSV, help="Output CSV path")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="HTTP request timeout in seconds")
    args = parser.parse_args(argv)

    http_client = HttpCatalogClient(args.url, token=args.token, timeout=args.timeout)
    if args.sync_snapshot:
        snap_path, ver = http_client.download_snapshot(args.snapshot)
        print(f"Synced snapshot {ver} -> {snap_path}")
        if not Path(args.metadata_db).is_file():
            return 0

    if not args.live and Path(args.snapshot).is_file():
        lookups = Lookups(SnapshotCatalogClient(args.snapshot, http_client=http_client))
        mode_desc = f"snapshot:{args.snapshot} ({lookups.version})"
    else:
        lookups = Lookups(http_client)
        mode_desc = f"http:{args.url}"

    books = read_calibre_books_readonly(args.metadata_db)
    if args.limit:
        books = books[:: max(1, len(books) // args.limit)][: args.limit]

    started = time.monotonic()
    rows = run_scan(
        books,
        lookups,
        progress=lambda n, total, _r: (
            print(f"{n}/{total}", file=sys.stderr, flush=True) if n % 100 == 0 else None
        ),
    )
    write_csv(rows, args.csv)
    counts = collections.Counter(row["bucket"] for row in rows)
    print(f"{len(rows)} books ({mode_desc}), {time.monotonic() - started:.1f}s")
    for bucket in BUCKETS:
        print(f"{bucket:22}{counts[bucket]}")
    print("csv:", args.csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# BL-Catalog for Calibre

A unified Calibre plugin providing both:

1. **Reading Lamp Toolbar Matcher (`BLCatalogPlugin` / `BLCatalogAction`)**: An interactive GUI dialog styled with BL-Catalog's Everforest **Reading Lamp** palette and warm brass book-and-skull icon. Scans selected books or the entire Calibre library, classifies matches into the PR #208 buckets (`certain_isbn`, `certain_title_author`, `picked`, `ambiguous`, `conflict`, `already`, `not_in_blc`, `error`), lets you inspect work details and manually pick anthology/collection works (with story-author support per D077), exports the 13-column dry-run CSV, and applies updates through Calibre's `db.new_api` with read-back verification and an append-only JSONL audit log.
2. **Metadata & Cover Source (`BLCatalogMetadataSource`)**: Automatically registered into Calibre's native **Download metadata and covers (`Ctrl+D`)** pipeline to fetch `bl_catalog` work IDs, ISBNs, titles, canonical authors, series, synopses, tags, and SHA-256-verified Lexicanum covers (preferring published Real-ESRGAN upscales).

## Building the plugin ZIP

Run the packaging script from the repository root:

```sh
python3 integrations/calibre/build_plugin.py
```

This syncs the canonical warm brass icon assets (`src/bl_catalog/api/web/icons/icon-v3-192.png` and `src/bl_catalog/api/assets/bl-catalog-warm-brass-icon.svg`) into `integrations/calibre/bl_catalog_calibre/images/` and writes:

```text
integrations/calibre/dist/bl-catalog-calibre.zip
```

The build also writes `bl-catalog-calibre.json` and `SHA256SUMS` beside the ZIP for GitHub releases.

## Plugin updates from GitHub

Plugin releases are published separately at [bl-catalog-calibre](https://github.com/jshauns81/bl-catalog-calibre/releases).
Open **Plugin Updates** in the matcher header, or **Check for Plugin Updates** in the toolbar menu.
Choose **Stable** for normal releases or **Beta** to include prereleases, then click **Check for Updates**.
The dialog shows release notes and asks before installing. Downloads are checked against the release
manifest, SHA-256, plugin identity and version, then passed to Calibre's plugin installer.
Quit and reopen Calibre after installation. Checks and downloads run in the background.

GitHub update requests use no catalogue bearer token or GitHub login. **Sync Snapshot** updates
catalogue data; **Plugin Updates** updates the plugin code. Existing installations need one manual
installation of version 1.1.0 or later to gain this updater.

Use version 1.1.1 or later for the bootstrap installation; it also preserves the chosen update
channel when metadata Apply options change.

You can also build directly with Calibre's CLI:

```sh
calibre-customize -b integrations/calibre/bl_catalog_calibre
```

## Installing in Calibre

### GUI installation

1. Open Calibre → **Preferences** → **Plugins** → **Load plugin from file**.
2. Select `integrations/calibre/dist/bl-catalog-calibre.zip`.
3. Restart Calibre when prompted and add **BL-Catalog** to your main toolbar if it is not placed automatically.

### CLI installation

```sh
calibre-customize -a integrations/calibre/dist/bl-catalog-calibre.zip
```

## Modes: Local SQLite Snapshot vs. Live HTTP API

- **Snapshot Mode (recommended for full-library scans)**: Click **Sync Snapshot** in the plugin header (or dropdown menu) to download `/v1/snapshots/latest.sqlite` atomically. The plugin opens the SQLite snapshot in strict read-only URI mode (`file:...?mode=ro`) and performs metadata matching locally without per-book HTTP requests. Performance depends on the library and host. Cover bytes are still fetched live from `/v1/work/{id}/cover` when applying covers.
- **Live HTTP API Mode**: Queries `/v1/work/{id}`, `/v1/isbn/{isbn}`, and `/v1/search` directly against the configured `BL_CATALOG_URL` (default `http://192.168.10.2:12404`), pinning `catalog_version` across each book's lookup budget.

## Guarded Apply & Field Toggles

By default, only **Identifiers (`bl_catalog` + missing valid `isbn`)** is checked, matching D078:

- Stamps `bl_catalog:w_...` on `certain_isbn`, `certain_title_author`, and manually `picked` books, and moves `already` stamps from merged BLC works to the surviving ID.
- Apply writes the selected rows, or with no selection the applicable rows visible under the current filters. It asks for confirmation with the book IDs first.
- Adds `isbn` only when the Calibre book has no valid ISBN of its own, preferring `ebook` editions first and never using `isbn_disputed` or audio-only editions.
- Optional checkboxes in the footer allow also applying **Title**, **Authors**, **Series**, **Synopsis**, **Tags**, and **Cover** (verified against `work.cover.sha256`).
- Every write re-reads live book state and refuses if the title, authors, or identifiers changed since the scan, or if another book in the library or the same batch claims the same `work_id`. It verifies read-back state after saving, rolls back partial writes, and appends a JSONL audit entry with previous field values to `/tmp/blc-calibre-apply.jsonl` (configurable). Replaced covers are kept in `blc-cover-backups/` next to the log.

## Standalone CLI Dry Run

The built `.zip` is also a runnable Python zipapp for read-only dry runs against a `metadata.db`:

```sh
python3 integrations/calibre/dist/bl-catalog-calibre.zip \
  --url http://192.168.10.2:12404 \
  --sync-snapshot \
  --metadata-db /mnt/unraid/Books/CalibreLibrary/metadata.db \
  --csv /tmp/blc-dry-run.csv
```

## Saved scans and manual snapshot updates

Match results are stored per library in `plugins/bl_catalog/library-scans.sqlite` under
Calibre's configuration directory. The active catalogue is also persistent, at
`plugins/bl_catalog/catalogue.sqlite`. Existing generated temporary snapshots are migrated
when available; custom snapshot paths are preserved.

Opening the matcher restores unchanged saved books without polling BLC, downloading a
snapshot or rematching the library. Apply is available for those matches immediately after
loading. Metadata reads use Calibre's cached format list rather than checking every ebook
file on disk. Newly added or edited books need Scan Selected or Scan Changes.

Snapshot Mode keeps the same catalogue until you explicitly choose **Sync Snapshot** and
confirm. Sync downloads and activates the latest snapshot in the background, keeps saved
matches, and does not start a scan. Saved matches from an older catalogue are marked **Saved**;
the tooltip identifies their version. Apply always rechecks only its requested books against
the active catalogue, checks current metadata and duplicate claims, and verifies each write.
Covers still download when requested. Live HTTP mode explicitly uses the live API instead.

**Scan Changes** compares current library metadata with saved fingerprints. With the same
snapshot it reuses unchanged results and matches only new/edited books. After you activate a
new snapshot it rematches the requested books against that version. **Force Full Rescan**
bypasses saved matches. Selected scans preserve other books' records. Deleted books are
pruned; interrupted scans keep the previous cache. Filters and field toggles use loaded data.
The snapshot title index resolves full normalized titles and aliases before loading work
metadata, avoiding hundreds of unrelated candidates for titles starting with “The”.

## Compact results table

The default table shows ID, title, up to two authors, match bucket and proposed changes.
Long text uses an ellipsis; hover for the full value, or select a row for the full inspector.
Enable **Show work ID and reason columns** for those extra columns. Column borders remain
resizable. The table and inspector share one rounded panel with an 8-pixel inset. The
Book details / Manual match pane stays 320 logical pixels wide; resizing gives the table
the remaining space. Column widths do not scan library contents during resizing; rows
stay single-line for predictable redraws.

## Filters and results after Apply

**Clear Filters** resets the bucket to All, clears search and turns off **Hide BLC-stamped books**.
The search field also has a clear icon. These controls only filter loaded rows; no scan is needed.
The hide toggle persists across dialog opens and hides books with an existing bl_catalog identifier.

After Apply, the plugin rereads live metadata and reclassifies only successful books, updates
bucket counts and proposals, and saves those refreshed rows to the persistent cache. Successful
rows show **Updated** with a timestamp and written fields in the tooltip. Newly stamped books
move into Already. Active filters remain in effect, so a successful book may leave a Certain
view or disappear when stamped books are hidden. Clear Filters reveals all loaded rows again.
Failed/rolled-back and unattempted books are not marked updated. Refresh/storage problems are
reported separately from successful writes. No Apply writes occur when clearing filters.

Snapshot downloads are checked for SQLite integrity, required tables and catalogue version
before replacing the active file. Each local matching operation holds one read transaction,
avoiding repeated filesystem lock traffic on network-backed configuration storage.

The command area and field controls adapt to the window width. Book details and manual
matching have separate tabs, and background operations show elapsed time and scan counts.

Successful cover writes save a receipt for the catalogue image and Calibre's stored image
(which Calibre may convert). Unchanged covers stop appearing as pending changes after Apply
or reopening. Selecting a book checks its current cover; Apply checks again before skipping
a cover write. External cover edits and new catalogue images remain eligible for replacement.
Unknown catalogue series positions do not propose clearing Calibre's required series index.

## Operation progress

Scan and Apply progress is shown as readable text above the progress bar. Apply identifies
the current book and stage (checking matches, downloading a cover, writing, verifying or
saving refreshed results), with processed/total counts and percentage. Refused, failed and
cancelled books count as processed; the final summary gives each outcome separately.
Elapsed time remains visible. Cancel stops after the current book rather than interrupting
a partial write. Percent text is not painted over the moving fill.


## Batch limit

The footer **Max** field accepts 1 to 5,000 books per Apply batch. Type a number directly
or use its arrows. Your chosen limit is remembered when editing finishes or Apply is clicked.
The confirmation still identifies the actual count and selected book IDs before any write.

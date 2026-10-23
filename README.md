# BL-Catalog for Calibre

A Calibre plugin for reviewing Black Library catalogue matches and applying selected metadata.
It includes a matcher dialog, a metadata/cover source, and GitHub-backed plugin updates.

## Install

1. Download `bl-catalog-calibre.zip` from [Releases](https://github.com/jshauns81/bl-catalog-calibre/releases).
2. In Calibre, open **Preferences → Plugins → Load plugin from file** and select the ZIP.
3. Restart Calibre and add **BL-Catalog** through **Preferences → Toolbars & menus** if needed.
4. Configure your BL-Catalog API URL using the plugin's **Configure BL-Catalog** menu entry.

The catalogue service is configured separately. This repository distributes the plugin, not catalogue data.

## Update

Open **Plugin Updates** in the matcher header or **Check for Plugin Updates** in its toolbar menu.
Choose **Stable** or **Beta**, click **Check for Updates**, review the notes, then choose **Install Update**.
Restart Calibre when ready. Checks and downloads run in the background.

Stable selects normal releases. Beta includes `vX.Y.Z-beta.N` prereleases and newer stable releases.
No GitHub login is required. Downloads are checked against the manifest, SHA-256, package identity
and embedded version before being passed to Calibre's installer. Catalogue credentials are not used.
Calibre's built-in public plugin index is a separate update mechanism; this plugin uses this repository's releases.

**Sync Snapshot** refreshes catalogue data. **Plugin Updates** refreshes plugin code.

## Review and Apply

Scans classify books into confirmed, ambiguous, conflicting, existing and unmatched results.
Metadata checkboxes choose what Apply may write. Toggling an option uses cached work data and
updates the existing table cells. Apply asks for confirmation and writes selected rows, or the
visible applicable rows when there is no selection, subject to the Max setting.

The default is identifiers only. Optional title, authors, series, synopsis, tags and cover changes
require selecting those fields. Writes verify live state and read-back results, with an audit log
and recovery of partial writes. Existing valid ISBNs are preserved.

## Development and publishing

Python 3.12 is used for the build and tests:

```sh
python -m pip install pytest ruff PyQt6
python -m pytest tests -q
python integrations/calibre/build_plugin.py
```

The builder creates the ZIP, `bl-catalog-calibre.json` and `SHA256SUMS` under
`integrations/calibre/dist/`. Version fields are in `integrations/calibre/bl_catalog_calibre/version.py`.

To release, set `PLUGIN_VERSION` and `RELEASE_TAG`, commit the changes, and push a matching
`vX.Y.Z` tag for Stable or `vX.Y.Z-beta.N` for Beta. The release workflow tests and validates
the package, then publishes the three assets. Beta tags are marked as prereleases and do not
replace the latest Stable release. Update channels follow numeric version ordering.

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
resizable, and the divider between results and inspector can be dragged. Column widths do
not scan library contents during window resizing; rows stay single-line for predictable redraws.

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

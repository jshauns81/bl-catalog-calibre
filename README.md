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

## Saved scans and delta matching

The matcher saves results in `plugins/bl_catalog/library-scans.sqlite` under Calibre's
configuration directory. In calibre-rdp this is
`/config/.config/calibre/plugins/bl_catalog/library-scans.sqlite`, backed by persistent appdata.
Each library path has its own records. Moving a library starts a new cache.

Opening the matcher restores unchanged books in a background job without contacting BLC.
Saved results show their last check time and catalogue version; a background check enables Apply automatically.
The first scan matches everything. Later scans compare current metadata with saved fingerprints,
reuse unchanged results, match added/edited books, and remove deleted books from the saved list.
Duplicate-work checks run over the combined results each time. Metadata options rebuild previews
from cached work data. Manual picks persist and are revalidated when catalogue data changes.

**Scan Changes** checks BLC's catalogue version. Snapshot Mode automatically downloads a newer
snapshot when needed; a new version rechecks all books, including previously unmatched titles.
If BLC is unavailable, saved results remain viewable, but a fresh check cannot complete.
**Force Full Rescan** bypasses saved automatic matches. Selected scans update only those books'
records. Interrupted scans leave the previous saved results intact. Cache failures are shown in
the status text and do not prevent a fresh scan. This database stores match results, not ebooks,
credentials, or Calibre's own metadata database. Existing guarded Apply checks remain in place.

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

## Opening a previous scan

Saved scans now check themselves automatically when the matcher opens. Existing matches
are restored first, then a background delta check verifies live metadata/catalogue freshness
for previously scanned IDs. Unchanged rows are reused; changed/outdated cached rows rematch.
Apply becomes available when this finishes, without clicking Scan Selected or Scan Changes.
The existing live write guards still run when Apply is confirmed. New unscanned books still
need a first scan; opening an old scan does not silently start matching the rest of a library.
If the check fails or is cancelled, saved rows remain visible and Apply stays blocked; use
Scan Changes to retry. The status explains this rather than requiring a scan every session.

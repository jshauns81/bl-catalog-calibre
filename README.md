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

# v1.3.0 acceptance: 2026-10-23

Tested in the actual Calibre 9.16 GUI on Linux through xrdp, against a real 1,882-book library. Selected metadata and original cover bytes were backed up before writes. These timings are observations on this host, not performance guarantees.

| Operation | Observed duration |
| --- | ---: |
| Read metadata through Calibre cache | 0.07 s |
| Restore saved matches | 2.36 s |
| Check changes, all 1,882 reused | 6.99 s |
| Full local match through controller | 8.27 s |
| Full rescan through native GUI, including progress/cache/table work | 13.64 s |
| Apply four real books through native confirmation | 4.8 s |
| Explicit snapshot sync, keeping saved matches | 9.7 s |

The full GUI scan delivered 134 timer heartbeats, with a maximum measured gap of 0.32 seconds. Window resizing remained active. At 1240×780 logical pixels (larger RDP scaling), the default results table had no horizontal overflow, and the inspector was 299×301 pixels. Two Title checkbox toggles over the full results took 0.56 seconds.

The four-book batch exercised identifiers, title, authors, series, synopsis, tags and verified covers. All four were written, moved into Already, and showed Updated. Direct database inspection confirmed stored identifiers/title; reopening retained the refreshed results. Repeat Apply performed no writes. Calibre converted three downloaded images; cover receipts correctly tracked the actual stored bytes rather than repeatedly proposing the original image.

Replacing one cover outside the plugin invalidated its receipt and offered a cover change; Apply restored the verified catalogue image. A deliberately injected read-back mismatch after actual writes to a fifth book triggered rollback. Its title, identifiers, authors, series and tags were restored, and the original cover was byte-identical. Synopsis HTML matched Calibre's normal comments adapter; six line breaks became spaces during the standard restore. The failed book was not marked Updated.

Clear Filters reset search, bucket and stamped-book hiding without a scan. Restoring saved matches and checking the four unchanged books worked with catalogue networking blocked, with zero attempted network calls. Explicit native snapshot confirmation downloaded and activated the snapshot, kept all 1,882 rows and the active filter, and did not start a scan.

Automated regressions additionally cover invalid snapshot preservation, stable reads across atomic replacement, cache/version/source boundaries, interrupted scans, stale writes, duplicate work claims, partial-write recovery, partial results after a later live-state/cover failure, external cover changes, nullable series positions, large-font layouts and Qt-rendered proposed fields.

## v1.3.1 progress follow-up

A second native confirmation applied two additional real books successfully. The UI displayed the current book, checking/downloading/writing/verifying stages, monotonic processed counts and percent outside the bar, then the saving-results phase. Each cover request was deliberately delayed by 1.5 seconds in the QA harness to inspect feedback during slow work; that delay is absent from the plugin. The two-book operation completed in 7.6 seconds with the injected delay. Both Updated rows persisted after reopening, with no remaining proposals or repeat writes.

Explicit runtime checks restored all 1,882 rows in 2.38 seconds, verified all six tested cover receipts, and reused the six unchanged books with catalogue networking blocked. The exact 95,177-byte installed v1.3.1 ZIP matches the tested package. Calibre's actual ZIP resource accessor retrieved the compiled arrow resource successfully. Dropdown and spinner arrows were visible in native RDP; Fusion and Windows pixel/click tests also pass.

Native Calibre runs Python with optimization enabled; the final QA harness uses explicit exceptions for checks so Python assertion removal cannot skip calls or validation. Public portable suite: 105 tests plus 22 updater subtests; private combined plugin suite: 158 tests plus 22 subtests. Ruff passes. Restart is deliberately deferred so the authenticated RDP session remains available.

## v1.3.2 shared results panel

Read-only Qt rendering used a copy of saved matches from the real 1,882-book library, under the existing palette and larger host fonts. Table and inspector now share one rounded frame with an 8-pixel inset. The inspector remains 320 logical pixels wide across 1240×780 → 1600×1000 → 1240×780, with no splitter. At 11 pt bold and 1240×780 the table viewport is 834×295 and details viewport 290×266; at 1600×1000 the table grows to 1194×545 while inspector width stays fixed. Default horizontal overflow is zero. All four outer corner samples retain the surrounding background; table children no longer paint over the curve.

The existing large-font layout, manual-match and progress tests pass. Independent checks cover 9 pt at the 1000×720 minimum, plus 11 pt bold and 12 pt at normal/wide sizes, both tabs, filtering, column resizing and keyboard focus. Manual-tab horizontal margins were reduced to keep its assignment button fully visible in the fixed pane.

A separate Qt preference check changed Max to 500, finished editing and reopened the dialog: 500 was retained, only max_apply was saved, the update channel stayed unchanged, and the existing 1–5,000 range remained. No real metadata writes were performed for this layout refinement.

The matcher window title identifies plugin release v1.3.2 separately from the active catalogue snapshot label. Toolbar code installed into a running Calibre process becomes active on a full Calibre restart; a temporary QA window opened yesterday was loaded explicitly for testing in that process.

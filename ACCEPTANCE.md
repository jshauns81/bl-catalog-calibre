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

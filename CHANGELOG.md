# v1.3.0-beta.1

- Restore saved matches without automatic catalogue checks, downloads or library rematching.
- Keep catalogue snapshots in persistent configuration; migrate generated temporary snapshots.
- Make snapshot adoption explicit, confirmed and asynchronous from both entry points.
- Keep saved matches usable after adoption; revalidate only requested books during Apply.
- Read Calibre's cached format list instead of checking ebook files on disk for every metadata read.
- Recognize the catalogue snapshot's actual version key.
- Index exact normalized titles and aliases before loading work metadata, avoiding unrelated candidates and the old 200-candidate limit.
- Preserve filter clearing, stamped-book filtering, post-Apply refresh and existing write guards.

## v1.2.4

- Check restored scan results automatically in the background when opening the matcher.
- Enable Apply for validated saved matches without a manual Scan Selected/Scan Changes step.
- Reuse unchanged saved matches and rematch changed/outdated cached books only.
- Keep Apply blocked with a visible retry explanation if validation fails or is cancelled.
- Leave new, unscanned books for their first explicitly requested scan.

## v1.2.3

- Add Clear Filters and a search clear icon; resetting filters does not scan the library.
- Add a persistent Hide BLC-stamped books toggle.
- Refresh written metadata, buckets, proposals and the saved cache immediately after Apply.
- Mark successful rows Updated, with the time and written fields in the tooltip.
- Preserve active filters and show refresh/cache failures separately from write results.
- Keep failed/rolled-back and unattempted books unmarked.

## v1.2.2

- Replace content-sized columns with stable widths and a stretching title column to reduce
  resize work on large libraries.
- Show at most two authors with an ellipsis/count; full text stays in tooltips and the inspector.
- Keep the five main columns visible by default; work IDs/reasons are optional table columns.
- Use single-line rows and compact change summaries; keep exact identifiers in tooltips.
- Give the table more room and wrap long status/inspector headings.
- Includes the synopsis compatibility fix and Apply diagnostics from v1.2.1.

## v1.2.1

- Prepare synopsis text with the installed Calibre version's comments adapter so normal
  whitespace conversion does not trigger a false read-back failure and rollback.
- Keep exact read-back verification and rollback for real content mismatches.
- Show the failed book, error, rollback status and unattempted count directly after Apply.
- Include expected and actual values in the audit log for a read-back mismatch.

## v1.2.0

- Save library match results in persistent SQLite storage, isolated by library.
- Restore saved results on opening; require Scan Changes to check freshness before Apply.
- Match only added/edited books when the catalogue is unchanged, prune deleted books,
  and recheck duplicate work claims across cached and changed results.
- Automatically refresh a newer BLC snapshot and rematch when the catalogue changes.
- Preserve manual picks, retry transient errors, and offer Force Full Rescan.
- Show cached/rematched counts; keep previous saved results if a scan is interrupted.

## v1.1.1

- Preserve the selected GitHub update channel when metadata Apply options change.
- Includes the GitHub updater and full-library refresh corrections described below.

## v1.1.0

- Add GitHub-backed Stable and Beta plugin update channels, release notes, verified downloads,
  installation confirmation and restart guidance.
- Fix full-library metadata-option freezes by reusing cached cells and suspending automatic
  column sizing during full table refreshes.
- Preserve selection and Apply scope across option changes, and clear identifier proposals
  when their checkbox is switched off.
- Include the Reading Lamp contrast, focus, disabled-state and empty-state corrections.

Existing manually installed versions need one installation of this ZIP to gain the updater.

# v1.3.4

- Roll back every written field and the cover when the post-write read-back fails, so a failed Apply leaves the book exactly as it was instead of changed with nothing to undo. Found by code review of the in-repo plugin source; no behaviour change for a successful Apply.

## v1.3.3

- Open the matcher as an independent, nonmodal window that can sit behind Calibre; the toolbar returns to the same open window.
- Highlight the corresponding Calibre book when clicking or moving through BLC result rows, including conflicts. Show in Calibre brings the main window forward.
- Preserve Calibre searches and virtual libraries; explain when they hide a requested book.
- Keep Scan Selected in sync with Calibre's current selection, while preserving BLC Apply selections.
- Retire the old matcher on library switches and shutdown, cancel pending work, and guard the current local write/read-back/rollback before detaching from its database.

## v1.3.2

- Put the results table and inspector inside one rounded panel with a small inset, keeping square table painting clear of the outer corners.
- Give Book details / Manual match a fixed 320-pixel logical width and remove the movable divider; the table takes remaining space.
- Keep the table's keyboard focus outline, native scrolling, column resizing and large-font manual controls usable.
- Show the plugin release in the matcher window title, separately from the catalogue snapshot version.
- Remember the footer Max batch limit across dialog opens. Its existing range remains 1 to 5,000 books.

## v1.3.1

- Show readable counts and percentage outside the progress fill.
- Report the current book and Apply stage while checking matches, downloading covers, writing metadata, verifying read-back and saving refreshed results.
- Keep progress and elapsed time visible during slow work; retain cancellation and per-outcome summaries.
- Give dropdown and spinner arrows visible artwork and larger controls under both Qt styles.

## v1.3.0

- Restore saved matches without automatic catalogue checks, downloads or library rematching.
- Keep the active snapshot in persistent configuration. Snapshot adoption is explicit and confirmed; existing matches remain usable, with only requested books rechecked on Apply.
- Read Calibre's cached format list, index exact normalized titles and hold one SQLite read transaction per operation to reduce network filesystem overhead.
- Validate downloaded SQLite data before replacing a good snapshot.
- Refresh successful writes even when a later cover download or live-state read fails.
- Remember verified stored covers, including Calibre conversions; avoid repeated cover proposals and writes while detecting later cover changes.
- Remove phantom series proposals when the catalogue has no series position.
- Reflow scan commands, filters, buckets and metadata options; separate book details from manual matching, and show elapsed time and scan counts.
- Keep long author lists compact, show readable proposed updates, and preserve Clear Filters, stamped-book hiding, Updated markers and write guards.

Native acceptance used Calibre 9.16 and a real 1,882-book library. Four selected books were written successfully; repeat Apply made no writes. An external cover edit was detected and repaired, and an injected read-back mismatch rolled back the real book. Timings depend on the host and library; see [acceptance notes](ACCEPTANCE.md) for measured results.

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

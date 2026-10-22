# v1.2.0

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

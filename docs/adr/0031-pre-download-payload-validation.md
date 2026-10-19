# ADR-0031: Validate torrent filenames before downloading payloads

- **Status:** Accepted — 2026-10-16
- **Decider:** LunchBox951
- **Issue:** [#609](https://github.com/LunchBox951/Plex-Management/issues/609)
- **Supersedes in part:** ADR-0017's torrent-payload-security and early-rejection non-goals. Its byte verification and conservative video policy remain in force.

## Context

An executable-only release downloaded completely and then remained in `import_blocked`, preventing subsequent episodes from progressing. Checking release titles cannot detect this. The operator has chosen to reject known executable types, download only video candidates, and keep payloads held when validation is unavailable.

## Decision

New qBittorrent adds use the `MetadataReceived` stop condition. A full torrent is added stopped; a magnet fetches metadata and then stops. The adapter requires WebAPI 2.11.0 or newer and refuses an unreadable or older capability response before adding; setup and health validation report an older client as failed. There is no unchecked fallback. A torrent the client already had receives no hold, so its live state decides whether it was running, as for a legacy row.

The download records a durable pending-validation flag. Reconciliation waits for a complete manifest and a confirmed stopped state, inspects every sibling, rejects known executable/script/installer/shortcut extensions, and requires at least one candidate from the existing Plex video policy, excluding the samples and extras folders that import skips. Dangerous suffixes are case-insensitive and checked on path components, including application bundles. Non-video sidecars are skipped rather than grounds for rejecting a healthy release.

The adapter applies file priorities using the client's file IDs, checks that the manifest has not changed, and reads the priorities back. Only confirmed video-only selection may release the hold. A temporary failure leaves validation pending for the next reconciliation. Restart recovery repeats this idempotent sequence; the pending flag clears only after the start call succeeds.

A confirmed rejection records its reason and history before invoking the existing blocklist-and-fail lifecycle. The reason survives failure recovery. Rejected rows remain visible in the queue for 24 hours; history retains the event thereafter. Requests and seasons re-enter the existing automatic search path. Existing no-video `import_blocked` rows are decided by their manifest, because the older scan and probe reported unreadable paths with the same reason. A manifest with video leaves the row retryable; a row whose torrent left the client is released to the import retry, which still verifies bytes. A probe failure on a file that cannot be read is an outage, not a verdict. New completed imports whose probes reject every candidate take the same rejection path. An empty disk scan does so only when the client's manifest also has no video; otherwise it stays a retryable block. Other import failures, including unavailable byte verification, retain their existing recovery rules.

The migration enrols existing unfinished downloads in validation. Already downloaded bytes cannot acquire a retrospective guarantee. Legacy magnets still acquiring metadata are stopped and removed without blocklisting, then re-enter the existing search/grab flow to acquire the new client hold. A durable restart reason allows recovery if the application stops during that transition. Running download intent is persisted before stopping for inspection; manually stopped downloads stay stopped after successful validation. Validation waits while the client is checking resume data or moving files. A stopped magnet without metadata is restarted like a legacy one. Rows importing with a placement breadcrumb are left to crash recovery. Missing-client rows remain enrolled when they reappear. If an unchecked torrent disappears, the existing missing-client grace period re-arms its request without blocklisting an uninspected release; the stored failure reason carries that verdict through crash recovery. Imports wait for that inspection. This change does not inspect or delete already imported libraries.

## Evidence and limits

The qBittorrent 5.0.4 [native torrent extension](https://github.com/qbittorrent/qBittorrent/blob/release-5.0.4/src/base/bittorrent/nativetorrentextension.cpp) pauses on leaving metadata acquisition. Its [torrent implementation](https://github.com/qbittorrent/qBittorrent/blob/release-5.0.4/src/base/bittorrent/torrentimpl.cpp) applies the stop condition before reload and includes it in saved resume data. Its [session implementation](https://github.com/qbittorrent/qBittorrent/blob/release-5.0.4/src/base/bittorrent/sessionimpl.cpp) adds torrents with existing metadata stopped under that condition.

An isolated qBittorrent 5.2.4 test with a local seeder confirmed metadata acquisition with zero payload bytes, preservation of the hold across a client restart, and completion only after an explicit start. Application tests cover manifest classification, selection confirmation, pending failures, and request recovery.

An extension is not proof of video content or safety. Existing ffprobe checks still inspect downloaded bytes before import. This is not antivirus, archive inspection, or a universal codec policy. BitTorrent pieces can span selected and skipped files, so skipped sidecars may contribute incidental bytes to required pieces. External operators controlling qBittorrent can override its holds; Plex Manager cannot enforce policy against a separately controlled client.

## Consequences

Grabs wait for metadata and validation before payload transfer. A held magnet that never receives metadata still follows the existing metadata-stall self-heal. A client outage or unsupported version can delay a request instead of downloading unchecked. The three additive database columns preserve compatibility with older application schemas, but rolling back the application removes enforcement of the new policy. No subtitle/artwork import or general payload scanner is introduced.

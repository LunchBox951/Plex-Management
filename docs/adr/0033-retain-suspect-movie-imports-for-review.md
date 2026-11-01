# ADR-0033: Retain suspect movie imports for review while replacement search continues

- **Status:** Accepted — 2026-10-31
- **Builds on:** [ADR-0010](0010-import-pipeline-honest-availability.md), [ADR-0017](0017-plex-video-download-validation.md), and [ADR-0031](0031-pre-download-payload-validation.md).

## Context

A falsely named release passed filename and playable-video checks and was imported as the requested movie. The file ran about 86 minutes while the requested movie's metadata listed 173 minutes. A wrong-media report then selected the same torrent from another indexer whose search result omitted its hash. Filename matching, container validation, and Plex catalogue matching cannot establish which film the footage contains.

The operator chose to flag strong runtime conflicts, retain an Import anyway action, and continue searching for another release. Missing metadata must not reject otherwise valid files. Alternate cuts and incorrect provider metadata require an explicit override rather than a permanent wrong-media verdict.

## Decision

Movie imports compare duration from the existing bounded ffprobe inspection with optional TMDB runtime metadata. A difference must be both at least 30 minutes and at least 40% of the expected runtime to trigger review. Missing, non-finite, zero, or unavailable runtime evidence leaves the existing import checks in force. This policy applies to movies; it does not introduce episode-runtime matching or a new recognition service.

A runtime conflict retains the downloaded file outside Plex. The download uses the existing terminal `no_acceptable_release` state with durable review metadata, freeing its active-download slot. Its request returns to `searching`. A linked automatic exclusion prevents the held torrent from being selected again while the queue presents its reason and an **Import anyway** action. Known and newly resolved torrent hashes are checked against the blocklist, including results from another indexer.

Import anyway approves only the runtime hold. Filename identity, quality, container, path, and publication checks still run. If another replacement is downloading, approval first stops it and confirms its stopped state, preserving its files. Stop failure or cancellation leaves both records retryable. The service rereads request and download state after client I/O and applies conditional database updates. Approval is refused if the request has settled or a replacement is already importing. It removes only the automatic exclusion linked to that review. Approval survives import retries and is reset when a terminal download is reused for a new grab.

Migration `3af8b3f3dbc5` adds review reason, approval, and linked-exclusion columns. It preserves the existing active-download unique index and introduces no new stored state or blocklist-reason values. Older applications can read the expanded schema, but do not provide the new review interface or runtime policy.

## Consequences

Large discrepancies receive an operator-visible correction path while replacement acquisition continues. The conservative threshold leaves ordinary runtime variation alone, and an operator can accept a legitimate alternate cut. An incorrect provider runtime can still delay a valid movie until that override.

Runtime agreement is not proof of identity. A falsely named movie with a similar duration can pass, and absent metadata cannot establish a mismatch. Existing executable-manifest and byte-validation checks remain necessary. This decision does not add antivirus scanning, perceptual recognition, or a claim that every wrong movie can be detected automatically.

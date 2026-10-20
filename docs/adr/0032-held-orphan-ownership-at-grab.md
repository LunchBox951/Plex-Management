# ADR-0032: Take ownership of a held orphan when a grab reuses it

- **Status:** Accepted — 2026-10-19
- **Decider:** LunchBox951
- **Issue:** [#620](https://github.com/LunchBox951/Plex-Management/issues/620)
- **Supersedes in part:** ADR-0031's sentence "A torrent the client already had receives no hold, so its live state decides whether it was running." The rest of ADR-0031 remains in force.

## Context

ADR-0031 adds every new torrent with the `MetadataReceived` stop condition and starts it after validation only when the download records that it should run. A grab records that intent only when its add created the torrent. A torrent the client already had (qBittorrent's 409, `created=False`) is judged by its live state.

A crash between the client add and the download row commit (the [#481](https://github.com/LunchBox951/Plex-Management/issues/481) window) leaves the app's own held torrent in qBittorrent with no row. A later grab of the same release receives `created=False`, records no running intent, and validation leaves the torrent stopped while its row says "downloading". Stopped states are excluded from stall detection, so nothing recovers it.

## Decision

When an add returns `created=False`, the grab reads the client's live state for that hash. It records running intent, so validation starts the torrent, only when both of these hold:

- no non-terminal download row tracks the hash;
- the torrent is `stoppedDL` or `pausedDL` with zero progress and a readable file manifest, or it is still fetching metadata (`metaDL` or `forcedMetaDL`).

That shape is the app's own held add from the crashed grab, and the current grab exists to download it. A metadata-fetching orphan therefore proceeds under its existing hold instead of taking ADR-0031's legacy remove-and-regrab path. Any other pre-existing torrent keeps ADR-0031's rule: its live state decides whether it was running. That includes a partially downloaded, completed, seeding, or still-tracked torrent.

`domain.payload_validation.is_unstarted_held_torrent` is the single definition of a held torrent the app may take ownership of. The operator Adopt action planned in #481 must use the same definition, so automatic and manual ownership cannot drift.

## Consequences

A regrab of a release whose earlier grab crashed after the add now downloads it after validation, instead of leaving it silently stopped. A client read failure during this check propagates as the same client error an add failure raises, so the grab records no row instead of guessing ownership.

An operator's identical, deliberately stopped copy of the same release at zero progress would be started by a grab of that release. The match is narrow enough that this is accepted.

This does not recover client-only torrents for a release that is never regrabbed, add durable pre-add intent (ADR-0029/0030), or add the Adopt and Remove controls; those remain #481's scope.

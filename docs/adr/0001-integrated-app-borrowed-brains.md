# ADR-0001: Integrated app with *borrowed brains* (Option C)

- **Status:** Accepted — 2026-06-29
- **Deciders:** LunchBox951 (owner)

## Context

The prototype collapsed the `Overseerr → Radarr/Sonarr → Prowlarr → qBittorrent`
stack into one app. It worked on the happy path but failed precisely at the
*arr stack's hardest-won logic: release-name parsing and quality selection. It
grabbed CAM/TELESYNC releases because its homegrown `scoring.py` /
`torrent_validator.py` only weakly approximated a real release parser and quality
profile. Three structural options were considered for v2 (see Alternatives).

## Decision

Build **one integrated, self-contained app that owns the entire brain** —
discovery, parsing decisions, quality, blocklist, import, retention, correction,
and UI — **but do not hand-roll the parser and quality model.** Instead, stand on
a **proven release-name parser** and a **Radarr-style ordered quality profile
with a hard cutoff**. The only runtime delegated to an external service is the
actual download (see [ADR-0006](0006-download-client-port-qbittorrent.md)).

CAM/TS/TELECINE releases are **rejected outright** by the quality profile, not
merely down-ranked — this is the structural fix for the prototype's defining bug.

## Consequences

**Positive**
- Keeps the project's reason to exist: simpler and more unified than running the
  full 5-service stack.
- The defining bug becomes structurally impossible without running extra services.
- Lightest operational footprint of the viable options (matters for the modest
  release host).
- Salvage: the prototype's qBittorrent/Prowlarr/Plex/TMDB clients and retention
  logic are reusable; only the broken parsing/quality/state layers are rebuilt.

**Negative / risks**
- Quality-profile, blocklist, and import logic are still ours to build (now on a
  solid parser foundation rather than from scratch).
- We depend on the maturity of the chosen parser library (selection deferred to
  v1 planning).

## Alternatives considered

- **A — Orchestrate the real *arr stack.** Your app drives Sonarr/Radarr/Prowlarr
  over their APIs. Lowest correctness risk, but reintroduces the exact 5-service
  stack the project exists to replace, overlapping heavily with Overseerr +
  Maintainerr. Rejected: defeats the project's purpose.
- **B — Integrated and fully reimplemented.** Purest vision, lightest footprint,
  but re-derives the single hardest body of logic in the ecosystem — exactly
  where the prototype broke. Rejected: reopens the wound.
- **C — Integrated app, borrowed brains.** Chosen: keeps the vision while
  amputating the risk.

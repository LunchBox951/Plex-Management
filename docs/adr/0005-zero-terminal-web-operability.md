# ADR-0005: Zero-terminal, web-operable release deployment

- **Status:** Accepted — 2026-06-29
- **Deciders:** LunchBox951 (owner)

## Context

The prototype's defining weakness was that recovery from any bad state required a
terminal: deleting files by hand, hand-editing SQLite, manually clearing
qBittorrent. The stable channel is expected to run on non-technical operators'
machines. Configuration also lived in magic numbers and a
hand-edited `.env`, and first-run setup was a CLI wizard.

## Decision

**The `:stable` deployment is 100% web-operable.** The terminal is an
**admin-only, install-time tool** — never required for *use, configuration,
recovery, or troubleshooting*. The single honest exception is the one-time
install (Docker + compose), which is an admin act performed once per host at
install time.

This principle is binding on v1 scope and *raises the floor*: the following stop
being "nice to have later" and become required because the deployment is
otherwise not operable without a terminal:

- **Web first-run setup wizard** (replaces the CLI wizard and hand-edited `.env`).
- **Settings UI** — every previously-hardcoded "magic number" is web-editable.
- **Health dashboard + in-app console/log viewer** — so troubleshooting never
  needs `docker logs` or `journalctl`.
- **In-app correction flows** — report-issue, re-search, force-grab, cancel,
  delete, blocklist management (the north star).

## Consequences

**Positive**
- The stable operator never needs a terminal.
- Directly drives concrete, testable v1 requirements rather than staying a slogan.

**Negative / risks**
- More surface to build in v1 (wizard, settings, health, console). Accepted as the
  cost of the principle.
- Remote admin support (the maintainer troubleshooting a stable deployment) is
  served by the in-app health/console for v1; richer remote support is deferred.

## Alternatives considered

- **CLI setup + file-based config (prototype model)** — rejected as the root cause
  of the very problem v2 exists to fix.

# ADR-0003: Ship as a Docker image via GHCR

- **Status:** Accepted — 2026-06-29
- **Deciders:** LunchBox951 (owner)

## Context

The top deployment requirement: a **package that can be built, shipped,
and auto-updated** on every host it lands on — from the maintainer's own canary
box to stable end-user installs. The prototype was deployed via git-pull + shell install
scripts + a venv + systemd, which drifted between hosts and required terminal
work. Python has historically been the weakest language at "shippable,
self-updating package."

## Decision

Ship Plex Manager as a **Docker image published to the GitHub Container Registry
(GHCR)**. Install with `docker compose up -d`. Configuration and the database
live in a **mounted volume** so updates and rollbacks never touch user data.

This makes the runtime language a free choice (the image bundles the interpreter
and all dependencies), which is what lets [ADR-0002](0002-python-typed-stack.md)
keep Python without paying Python's packaging tax. It also mirrors how the *arr
apps the project draws from are distributed.

## Consequences

**Positive**
- Install = one command; ship = CI pushes to GHCR; update = auto-pull; rollback =
  pin an older tag.
- **Identical environment on every host** — eliminates the prototype's
  "works on my host" drift (also a reliability win).
- Enables tag-based release channels (see [ADR-0004](0004-edge-stable-release-channels.md)).
- GHCR is free with the project's GitHub account.

**Negative / risks**
- Requires a **container runtime on every host, including stable end-user installs**.
  This is a one-time admin setup; acceptable because install is explicitly an
  admin act (see [ADR-0005](0005-zero-terminal-web-operability.md)).

## Alternatives considered

- **Native single binary + GitHub-release self-update** — lightest footprint, no
  container runtime; Go's home turf (or Python via Nuitka/PyInstaller). Rejected:
  pushes away from Python (or adds bundling fragility), and loses the
  "promote the exact tested artifact" guarantee.
- **Distro package (`.deb`) + private apt repo** — most OS-native; `apt upgrade`
  updates. Rejected: requires maintaining a signed apt repo, bundling a Python
  venv into a `.deb` is the fiddliest option, and each channel re-packages rather
  than promoting identical bytes.

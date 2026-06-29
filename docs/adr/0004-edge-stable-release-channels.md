# ADR-0004: `:edge` / `:stable` channels by tag promotion

- **Status:** Accepted — 2026-06-29
- **Deciders:** LunchBox951 (owner)

## Context

The project runs deployments at two maturities: a small **pre-release (beta)**
fleet rides a fresh build to catch bugs early (the canary), while **stable**
deployments only ever receive a **polished, promoted release**. This is a classic
beta/stable release-channel strategy, and the project already ships as a tagged
Docker image (see [ADR-0003](0003-docker-ghcr-packaging.md)).

## Decision

Model channels as **image tags with a manual promotion gate**:

- Every merge to `main` builds and pushes **`:edge`** plus an immutable
  **`:edge-<sha>`** tag. The **canary fleet** (the beta channel) auto-pulls
  `:edge`; the `:edge-<sha>` tag is the precise handle the promotion step consumes.
- When an edge build has proven itself, the maintainer **promotes** it by
  **re-tagging that exact image (by reference) as `:stable`** and the versioned
  `:x.y.z` (and `:x.y`) — *no rebuild*. **Stable deployments** auto-pull
  `:stable`. Releases are produced only by promotion, never by a separate
  `main`-independent build.

Because promotion re-tags rather than rebuilds, every stable deployment runs a
**bit-identical artifact** to what was validated on the canary — including any
database migration that already ran cleanly there first. The canary is the
blast shield for release *and* migration bugs.

## Consequences

**Positive**
- Stable deployments provably run what was tested on the canary, not a fresh build.
- Migrations are de-risked: they execute on the canary before any stable host.
- Rollback is re-pointing a tag.
- Promotion is a deliberate, maintainer-controlled act — not automatic.

**Negative / risks**
- Requires CI to publish `:edge` and a separate, gated promotion workflow for
  `:stable`. Modest extra pipeline complexity.
- The canary may occasionally run a broken build — that is the point, and it is a
  beta deployment that opted into pre-release, not a stable user.

## Alternatives considered

- **GitHub Releases pre-release flag (native binary)** or **apt `edge`/`stable`
  suites (`.deb`)** — both express channels but re-build/re-package per channel,
  forfeiting the "promote the identical tested artifact" guarantee. Rejected with
  [ADR-0003](0003-docker-ghcr-packaging.md).

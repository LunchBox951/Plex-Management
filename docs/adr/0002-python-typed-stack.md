# ADR-0002: Strictly-typed Python stack

- **Status:** Accepted — 2026-06-29
- **Deciders:** LunchBox951 (owner)

## Context

The prototype was Python/FastAPI. Several production bugs were the kind a stronger
type discipline catches at author time (e.g. `'QBittorrentClient' object has no
attribute 'torrents_info'`, untyped dict-shuffling, bare `except` swallowing
errors). The owner is most comfortable in Python but open to another language if
it better serves the goal. The load-bearing requirement from
[ADR-0001](0001-integrated-app-borrowed-brains.md) is *borrowed brains*: a proven
release parser and quality model. Packaging (see
[ADR-0003](0003-docker-ghcr-packaging.md)) makes the runtime language a free
choice, so language is decided on fit, not deployment.

## Decision

Use **Python 3.12+, strictly typed**, with `pyright --strict` enforced in CI.

- **Web:** FastAPI + Pydantic v2.
- **Persistence:** typed SQLAlchemy 2.0 + Alembic (see [ADR-0007](0007-sqlite-alembic-migrations.md)).
- **Tooling:** `ruff` (lint + format + bandit `S` rules), `pyright`, `pytest`, `pre-commit`.

## Consequences

**Positive**
- The best-in-class release parsers live in Python (`guessit`,
  `parse-torrent-title`, `RTN`) — directly serving the borrowed-brains decision.
- Salvages the prototype's working clients and domain logic.
- Owner familiarity → faster, more maintainable iteration.
- The reliability gains we want come from **architecture** (a reconciler, a typed
  state machine, no silent `except`) plus strict typing — available without a
  language change.

**Negative / risks**
- Python's dynamism still allows silent failures if discipline lapses; mitigated
  by `pyright --strict`, Pydantic models at all boundaries, and a no-bare-`except`
  review rule.
- Heavier runtime than a single native binary — neutralized by containerization.

## Alternatives considered

- **TypeScript end-to-end** — strong types and shared front/back types, but a full
  rewrite (no salvage) and a thinner combined parse-*and*-rank story than Python's
  `RTN`. Viable; not chosen.
- **Go** — best native single-binary deploy and excellent for the concurrent
  reconciler, but the weakest release-parser ecosystem, which would force us to
  port Radarr's regexes ourselves and partially reopen the
  [ADR-0001](0001-integrated-app-borrowed-brains.md) risk. No prototype salvage.
  Rejected because containerization already solves the deploy story that was Go's
  main draw.
- **Rust / C#** — Rust is overkill for a home media app; C# could lift the *arr
  parser verbatim but is heavy and outside the owner's wheelhouse. Rejected.

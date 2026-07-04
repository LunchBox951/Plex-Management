# Backend Alpha — historical first-slice plan

> Historical note: this document records the first backend-alpha planning slice.
> It is not the current shipped-scope statement; use the README, changelog, ADRs,
> and exported OpenAPI contract for current behavior.

- **Status:** In progress (first backend-alpha session)
- **Goal:** the smallest slice that proves the v2 architecture and the prototype's
  defining-bug fix, and hands the front-end team **real, typed REST contracts** to
  build against. Front end is out of scope this session.

This is a planning artifact for the alpha; per [issue #3](https://github.com/LunchBox951/Plex-Management/issues/3)
it is removed at the v1 cleanup. The durable decisions live in the ADRs.

## The alpha's right edge

The **request → search → grab** path, stopping before file import:

> create request → resolve metadata (TMDB) → search indexers (Prowlarr) →
> **parse (guessit) → quality gate with hard cutoff → blocklist filter → score/sort**
> → preview ranked candidates (with per-release rejection reasons) → grab the top
> candidate into qBittorrent → the reconciler tracks its status.

This is enough to prove the hexagon (ports + adapters), the headline
**CAM/TS/TELECINE structural fix** ([ADR-0001](../adr/0001-integrated-app-borrowed-brains.md),
[ADR-0008](../adr/0008-release-parser-guessit.md)), and the durability model
(one reconciler, a history table, a blocklist), while publishing an OpenAPI
contract for the front end.

**Deferred** (ports defined, adapters stubbed — no wired pipeline yet): file
import (validate → rename → route → Plex scan), Plex availability dedupe,
disk-pressure eviction, retention, Plex OAuth, notifications, in-app console.

## Architecture (recap)

Ports-and-adapters. The `domain/` core is pure (no I/O, no adapter imports) and
fully unit-testable: the decision engine, the request/download state machine, and
the **pure reconciler** `reconcile(db_rows, client_items) -> transitions`. Adapters
(`guessit`, Prowlarr, qBittorrent, TMDB) satisfy typed ports. SQLite via async
SQLAlchemy 2.0; schema owned by an Alembic migration ([ADR-0007](../adr/0007-sqlite-alembic-migrations.md)).

## Key decisions taken this session

| Decision | Choice |
|---|---|
| Release parser | `guessit` (parse-only) behind `ParserPort`; ranking stays in the domain — [ADR-0008](../adr/0008-release-parser-guessit.md) |
| Quality model | Radarr-style ordered profile + **hard categorical cutoff**; CAM/TS/TC/WORKPRINT/DVDSCR/SCREENER rejected, never down-scored |
| I/O model | async end-to-end (`aiosqlite` + `httpx.AsyncClient`); migrations run sync |
| Auth (alpha) | static `X-Api-Key` header vs `SystemSettings.app_api_key`; dev bypass; no Plex OAuth yet |
| Secrets | Fernet key-file at `data/secret.key`, encrypted at rest, never logged — [ADR-0005](../adr/0005-zero-terminal-web-operability.md) |

## REST surface (the front-end handoff)

Setup wizard (`/api/v1/setup/validate/{service}`, `/complete`, `/status`),
settings, TMDB discovery search, requests, **`/search-preview`** (decision-engine
dry run: ranked candidates + rejection reasons + `NoAcceptableRelease`), queue
(downloads + reconciled status), blocklist, quality-profile. The exported
OpenAPI JSON (`docs/api/openapi.json`) is the source of truth for the contract.
All routes except setup + `/health` require the API key (unless dev bypass).

## Quality gates

`ruff check` · `ruff format --check` · `pyright --strict` · `pytest` — green at
every step. CI also runs the suite on the 3.12 floor and the 3.14 image runtime.

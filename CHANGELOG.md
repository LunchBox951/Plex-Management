# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Backend alpha — the request → search → grab slice:**
  - Pure domain decision engine: `guessit` parsing behind a `ParserPort`, a
    Radarr-style ordered quality profile with a **hard categorical cutoff** that
    rejects CAM/TS/TELECINE/WORKPRINT/DVD-screener releases outright, a blocklist
    filter, and scoring — surfacing "no acceptable release" instead of failing
    silently ([ADR-0008](docs/adr/0008-release-parser-guessit.md)).
  - Async SQLAlchemy 2.0 persistence (10 typed models) with the initial Alembic
    migration; secrets encrypted at rest (Fernet); SQLite FK enforcement enabled.
  - Live adapters for Prowlarr (search), qBittorrent (grab + status), and TMDB
    (discovery), behind typed ports; Plex/filesystem ports defined.
  - A download state machine and a pure reconciler that heals client↔DB drift
    (a missed poll never falses a download).
  - FastAPI REST API: setup wizard, `X-Api-Key` auth + setup guard, discovery,
    requests, `search-preview`, queue (grab/reconcile/mark-failed), blocklist,
    and quality-profile — with a published OpenAPI contract (`docs/api/openapi.json`).
- Project foundation: design overview and ADR-0001..0008.
- Repository base: README, license (MIT), security policy, contributing guide,
  code of conduct.
- CI pipeline: lint/format/test (3.12 + 3.14), CodeQL, dependency & secret
  scanning, container build + image scan, and a manual `:stable` promotion workflow.

_No released versions yet. The alpha covers search → grab; file import, Plex
dedupe, retention, and the front-end are deferred._

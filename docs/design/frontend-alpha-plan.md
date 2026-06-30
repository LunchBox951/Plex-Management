# Frontend Alpha — scope & plan

- **Status:** In progress (first frontend-alpha session)
- **Goal:** the smallest UI that drives the backend alpha's
  **request → search → grab** loop end-to-end through a real, web-operable
  interface — and establishes the durable frontend foundation
  ([ADR-0009](../adr/0009-frontend-typed-spa.md)).

This is a planning artifact for the alpha; like [`alpha-plan.md`](alpha-plan.md)
it is removed at the v1 cleanup ([issue #3](https://github.com/LunchBox951/Plex-Management/issues/3)).
The durable decision lives in ADR-0009. Design source: the Claude Design handoff
([issue #7](https://github.com/LunchBox951/Plex-Management/issues/7)) — *a
template, not a source of truth; engineering docs win on conflict.*

## The alpha's right edge

A typed React SPA, served by the FastAPI app, covering both faces of the product
against **only endpoints that actually exist** today:

> first-run **setup wizard** → **Discover** (real TMDB search) → **request** a
> title → **search-preview** (ranked release candidates + per-release rejection
> reasons, incl. `NoAcceptableRelease`) → **grab** → **live queue** (polled
> progress) → **correction** (mark-failed / blocklist), plus **settings**,
> **blocklist** and a read-only **quality profile** view.

This proves the hexagon through a UI, exercises the headline CAM/TS structural
fix visibly (rejection reasons are surfaced, never silently dropped), and honours
the zero-terminal north star ([ADR-0005](../adr/0005-zero-terminal-web-operability.md)).

**Deferred** (no backend yet — left as typed `TODO`s, not faked as if real): a
composed Discover *home* (`/discover/home`, trending/affinity rows), a live event
stream (SSE/WebSocket — we poll), Plex-OAuth identity (`/me`, avatars), poster
art on requests/queue rows (only `/discover/search` returns art today), and TV
season/episode scoping.

## Stack (ADR-0009)

Vite · React 19 · TypeScript (strict) · TanStack Query · Tailwind CSS v4 ·
Radix UI · React Router. The API client is **generated** from
`docs/api/openapi.json` (`openapi-typescript` + `openapi-fetch`); `make
gen-client` regenerates it and CI fails on drift. Node lives only in a build
stage; the runtime image stays `python:3.14-slim` and Node-free.

## Screens & the endpoints they bind to

| Screen | Purpose | Endpoints (`/api/v1` + `/health`) |
|---|---|---|
| **Setup wizard** | First-run: validate each service, then complete; store the minted API key | `GET setup/status`, `POST setup/validate/{plex,prowlarr,qbittorrent,tmdb}`, `POST setup/complete` |
| **Discover / Search** | Search TMDB; request a title; detail modal as the request entry point | `GET discover/search`, `POST requests` |
| **Title detail → Search-preview** | Decision-engine dry run: ranked `accepted[]` (score/resolution/source/seeders/indexer) vs `rejected[]` (reason) vs `no_acceptable_release`; pick & grab | `POST search-preview`, `POST queue/grab` |
| **Requests** | List requests with their status | `GET requests`, `GET requests/{id}` |
| **Queue** | Live downloads (progress 0–1, polled), correction actions | `GET queue` (poll), `POST queue/{id}/mark-failed?blocklist=` |
| **Settings** | View (redacted) + edit service config | `GET settings`, `PUT settings` |
| **Blocklist** | List / remove entries | `GET blocklist`, `DELETE blocklist/{id}` |
| **Quality profile** | Read-only ordered profile with the hard cutoff | `GET quality-profile` |
| **Health** | Liveness indicator | `GET /health` |

Status/enum strings (`RequestStatus`, `DownloadState`, `QualitySource`,
`RejectionReason`, …) come from the generated types — the UI never re-declares
them. The handoff's 6-state per-title contract maps onto `RequestStatus`.

## Architecture

```
frontend/                 # SPA source (Node project; NOT shipped to runtime)
  src/
    api/                  # generated client (gen-client) + thin query hooks
    components/           # StatusBadge, PosterCard, ProgressBar, Dialog, Toast, …
    routes/               # wizard, discover, requests, queue, settings, blocklist
    lib/                  # api-key store, 409->wizard redirect, polling config
    styles/               # Tailwind theme = handoff design tokens
  index.html · vite.config.ts · package.json · tsconfig.json
src/plex_manager/web/
  static/                 # Vite build output (gitignored in dev, built in CI/Docker)
  routers/ui.py           # serves index + SPA fallback (NEW)
  app.py                  # mount StaticFiles + ui router (EDIT)
  middleware.py           # allowlist the SPA asset prefix + index pre-init (EDIT)
```

### Backend wiring (minimal, typed, tested — not feature work)

These are the integration points to *mount* the SPA; each ships with a test so
the `pyright --strict` + `pytest` gate stays green:

1. **Serve the SPA.** Mount `StaticFiles` for built assets and add a router that
   returns `index.html` for non-API paths (client-side routing fallback).
2. **Setup-guard allowlist.** Add the asset prefix + the SPA index to
   `SETUP_ALLOWLIST_PATHS/PREFIXES` so the wizard is reachable pre-init (the
   middleware already references `setup_path: "/setup"`).
3. **Dev ergonomics.** Document `PLEX_MANAGER_DEV_AUTH_BYPASS=true` for local runs.

Any *backend behaviour* gap found while wiring is left as a `TODO` and reported,
not silently worked around (per session rules); a true blocker gets its own MVP
PR from an isolated worktree.

### Build & packaging

- `make gen-client` → regenerate the typed client from the committed OpenAPI JSON.
- `npm run build` (in `frontend/`) → emits to `src/plex_manager/web/static/`.
- Dockerfile gains a `node:* AS web` build stage that runs the build and copies
  `dist/` into the runtime stage. Runtime image unchanged otherwise.
- CI gains: `npm ci`, typecheck (`tsc --noEmit`), lint, Vitest unit tests, the
  **gen-client drift check**, and a Playwright smoke. Python gates untouched.

## Quality gates

Python: `ruff check` · `ruff format --check` · `pyright --strict` · `pytest`
(unchanged, must stay green). Frontend: `tsc --noEmit` · eslint · `vitest` ·
`openapi` drift check · Playwright smoke. The contract drift check is the gate
that makes the typed-contract promise real.

## Build sequence

1. **Scaffold** the Vite/React/TS project + Tailwind theme (design tokens) +
   ADR/spec (this doc).
2. **Generate the client** and stand up the typed query hooks + api-key store +
   `409 → wizard` handling.
3. **Wire serving** (static mount + SPA fallback + allowlist) with backend tests.
4. **Build screens** in dependency order: setup wizard → discover/search +
   request → search-preview + grab → queue (poll) + correction → requests →
   settings → blocklist → quality profile.
5. **CI/Docker**: Node build stage + frontend gates.
6. **Adversarial review loop**, then live browser-driven smoke against real
   services, then PR.

# ADR-0009: Frontend — a typed SPA (Vite + React + TypeScript), contract-bound to the API

- **Status:** Accepted — 2026-06-30
- **Deciders:** LunchBox951 (owner)
- **Resolves:** the open frontend question parked in
  [`overview.md` §11.3](../design/overview.md) ("server-rendered vs light SPA").

## Context

The backend alpha ([#8](https://github.com/LunchBox951/Plex-Management/pull/8))
shipped a strictly-typed FastAPI REST surface and exports its contract to
[`docs/api/openapi.json`](../api/openapi.json). No UI is served yet. The design
overview deliberately left the frontend approach open; this ADR closes it.

The choice was evaluated against the qualities this project actually optimises
for over its lifetime:

1. **The app is two things at once** — a cinematic *consumer* browser
   (Discover, search, request, detail) **and** a heavy *operability* console
   (setup wizard, settings, health, console, the live download queue, and the
   in-app **correction** flows that ADR-0005 makes mandatory). The consumer half
   is SPA-shaped (deep-linked modals, infinite scroll, search-as-you-type); the
   admin half is form/table-shaped.
2. **Contract-first is the project's signature bet.** The backend is
   `pyright --strict` with a green-gate culture, and the stated goal of the
   backend alpha was to "hand the front end real, typed REST contracts."
3. **Live status is polling today.** There is no SSE/WebSocket yet; `/queue` and
   `/requests` are polled. The design *wants* a real event stream later.
4. **Single-maintainer, security-sensitive, single-image deployment.** The repo
   surfaces every Trivy/CodeQL/Dependabot/gitleaks finding and promotes
   bit-identical `:edge → :stable` images (ADR-0004).

Three options were steelmanned and scored against weighted criteria
(maintainability, supply-chain simplicity, type-safety, UX now and at scale,
live-update fit, test reuse, time-to-alpha, ecosystem, reversibility): a typed
SPA, a server-rendered Jinja2 + HTMX UI, and a no-build vanilla UI. HTMX scored
marginally highest on *current* priorities, but only the typed SPA **cashes the
OpenAPI contract at compile time** and fits the SPA-shaped consumer surface that
is part of the product's destination — the deciding factors for a *foundational,
hard-to-reverse* choice.

## Decision

**Build the UI as a typed single-page application, generated against the OpenAPI
contract, and serve it as static assets from the existing FastAPI app.**

- **Stack:** Vite + **React 19** + **TypeScript** (`tsc` strict) +
  **TanStack Query** (server state) + **Tailwind CSS v4** + **Radix UI**
  primitives (accessible modals/dialogs/toasts for the destructive correction
  flows). Routing via React Router.
- **The contract is the seam.** `openapi-typescript` generates types from
  `docs/api/openapi.json`; `openapi-fetch` gives a fully-typed client. A
  `make gen-client` target regenerates it and **CI fails if the committed client
  drifts from a fresh generation** — a backend field/enum rename becomes a red
  build, not a production bug. This extends the `pyright --strict` discipline
  across the wire.
- **Node never enters the runtime image.** A multi-stage Dockerfile builds the
  SPA in a throwaway `node:*` stage and copies only the static `dist/` into the
  `python:3.14-slim` runtime under `src/plex_manager/web/static/`. hatch already
  packages `src/plex_manager`, so the assets ship in the same image and
  bit-identical promotion (ADR-0004) is preserved. npm lives only in CI, where
  Dependabot/Trivy understand `package-lock.json`.
- **Live updates are polling now, push later.** TanStack Query `refetchInterval`
  drives `/queue` and `/requests`. When the backend grows an event stream, an
  `EventSource` handler writes into the query cache and the interval is removed —
  no component rewrite.
- **The SPA cooperates with the setup guard.** `SetupGuardMiddleware` already
  anticipates a client (`setup_path: "/setup"`). The static-asset prefix and the
  SPA index are added to the pre-init allowlist so the wizard loads before
  first-run setup completes; the typed client centralises the `409 setup_required`
  → wizard redirect.

## Consequences

**Positive**
- End-to-end type safety: the same contract types the backend and the frontend;
  drift is a CI failure.
- A real component model: the 6-state status badge, poster card, progress bar,
  modal, and confirm dialog are built once and reused across the consumer and
  admin surfaces — the structural fix for the prototype's per-page-CSS and
  hand-rolled state-sync pain.
- Matches Overseerr (the proven reference for this app category) so its patterns
  can be borrowed rather than re-derived (the ADR-0001 "borrow proven brains"
  spirit, applied to the UI).
- Runtime image and promotion model are unchanged (Node-free).

**Negative / risks (accepted)**
- A real second language and toolchain (TypeScript + Node/npm/Vite) the
  maintainer keeps green alongside the Python gates, plus periodic framework
  major-version migrations.
- A new npm dependency/CVE surface in CI (mitigated: confined to the build stage;
  scannable via `package-lock.json`; absent from the runtime image).
- A Node build stage adds CI time and a new build failure mode.
- UI unit logic wants a JS test runner (Vitest/Testing-Library) in addition to
  the Python `pytest` gate; end-to-end coverage uses Playwright.

**Reversibility.** The clean REST contract keeps the frontend swappable: the
generated client and design tokens port to any TS framework, and nothing in
`domain`/`ports`/`adapters` depends on the UI. Choosing a plain Vite SPA (not
Next.js/RSC) deliberately avoids the deepest framework lock-in.

## Alternatives considered

- **Server-rendered Jinja2 + HTMX (+ Alpine).** Highest single-maintainer and
  supply-chain simplicity, reuses the `pytest` gate, and is an excellent fit for
  the admin/forms surface. Rejected as the *foundation* because type safety stops
  at the template boundary (the OpenAPI contract becomes decorative for the UI),
  and the live dashboard + SPA-shaped consumer browser push it into untyped,
  unscanned client JS — discarding the project's two signature investments at the
  largest surface. Remains the natural choice **if a no-JS-build constraint ever
  becomes hard**; the REST contract keeps that retreat cheap.
- **No-build vanilla (ES modules + `lit-html`).** Lowest footprint and lifts the
  design verbatim, but re-introduces the hand-rolled cross-view state-sync that
  sank the prototype and has no enforced type story. Dominated by the other two.

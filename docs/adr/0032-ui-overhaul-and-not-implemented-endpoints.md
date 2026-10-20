# ADR-0032: UI overhaul — watching and Manage modes, and 501 for planned endpoints

- **Status:** Proposed — 2026-10-20
- **Decider:** LunchBox951
- **Design reference:** the "Plex Manager" design system (tokens, component cards, and the Interactions page) agreed during the overhaul brainstorm.

## Context

The v1 web UI was a prototype: a header with a "Discover / Requests" zone plus a row of admin links, desktop-first layouts, and a TV request flow that could only ask for one season or the whole series. Family members use the app mostly from phones; the operator needs every correction reachable without a terminal (north stars #1 and #2). The overhaul is landing UI-first: the first draft ships the screens, and the backend pieces some of them need follow as stacked PRs.

## Decision

1. **Two modes under one header.** *Watching* (everyone): Home, Movies, TV, My requests, search, notifications. *Manage* (admins only): a gold Manage button switches the header into Manage mode, which adds a row of tabs (Needs attention, Requests, Downloads, Blocklist, Quality, Settings, Logs, System health). No sidebar. Pre-overhaul admin paths (`/queue`, `/status`, …) redirect to their `/manage/…` homes.
2. **Phones first.** Below 640px a bottom tab bar replaces the header nav (Home, Search, My requests, Notifications, plus Manage for admins); menus become bottom action sheets and dialogs become bottom sheets.
3. **Interactions are one click, one result.** Clicking a thing opens it; buttons act. No keyboard shortcuts (the search `/` shortcut and its key-cap hint are removed). Reversible actions run at once with a toast offering Undo; anything that deletes files, blocklists a release or affects other people opens a confirmation that lists its side effects first. Fixes are admin-only; the person who asked gets "Let the admin know".
4. **Planned endpoints answer 501.** Where a screen needs backend behaviour that has not landed, the endpoint exists with its final request/response contract and its service raises `NotImplementedError` with a user-facing sentence. `web/errors.py` renders that as a 501 `not_implemented` envelope (`detail`, `message`, `hint`). The SPA checks `isNotImplemented(error)` and shows an honest "not available yet" state instead of a generic failure. Nothing is written by a 501 path.

## Consequences

- The client contract for notifications, "Let the admin know", "Import anyway" and the `latest_season` / `new_episodes` TV modes is fixed now, so the backend can land behind it without UI churn. Each stub names what the real implementation must do in its module docstring.
- A `NotImplementedError` raised by accident elsewhere would also surface as a 501 "not available yet" rather than a 500. That is still an honest, visible state; the handler is not a substitute for finishing a feature, and every intentional stub is listed in the PR that introduced it.
- Existing screens (Downloads, Blocklist, Quality, Settings, Logs, System health) move under Manage unchanged in this step; restyling them is follow-up work.

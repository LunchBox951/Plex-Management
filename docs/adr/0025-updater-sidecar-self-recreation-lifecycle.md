# ADR-0025: Automatic recreation of the updater sidecar (successor-spawn self-refresh)

- **Status:** Proposed
- **Date:** 2026-07-14
- **Proposes to resolve:** [issue #299](https://github.com/LunchBox951/Plex-Management/issues/299)
  (deferred from PR #280's Codex P2 finding).
- **Context builds on:** [ADR-0024](0024-first-party-container-auto-updater.md)
  (first-party updater sidecar, app-owned policy, one-target authority envelope),
  [ADR-0004](0004-edge-stable-release-channels.md) (moving `:edge`/`:stable` tags
  promoted by re-tag), [ADR-0003](0003-docker-ghcr-packaging.md) (Compose is the
  deployment unit across Linux/NAS/Desktop), [ADR-0005](0005-zero-terminal-web-operability.md)
  (the `:stable` deployment is 100% web-operable), and
  [ADR-0023](0023-database-rollback-and-pre-migration-backup.md) (rollback policy).
- **Relates to:** the coordinator phase-guard contract hardened in PR #346
  (issue #322) and the wedged-phase web-recovery work tracked in
  [issue #354](https://github.com/LunchBox951/Plex-Management/issues/354).

## Context

ADR-0024 ships an opt-in updater sidecar (`plex-manager-updater`) that inspects,
pulls, and recreates the labeled `plex-manager` container. Both containers start
from the same moving `PLEX_MANAGER_IMAGE` tag, but the update state machine
(`src/plex_manager/updater/runner.py`) only replaces the one target bearing the
`update.target` + `update.image-ref` labels. Pulling a tag never re-resolves the
already-running sidecar: `restart: unless-stopped` restarts the *same image ID*,
and Docker's restart policy never re-pulls. After the first automatic update the
**only Docker-socket-holding container in the deployment** keeps executing its
original bytes indefinitely. Later releases' updater-side security fixes and
coordination-protocol changes (for example the phase-guard contract from PR #346)
never reach the privileged process until an operator recreates it by hand.

Today's documented mitigation is a terminal command on the install host
(`docker compose --profile auto-update up -d updater`). That is exactly the class
of maintenance north star #1 ("correction without a terminal") and north star #2
("the `:stable` deployment is 100% web-operable") say must be a button, not a
shell. A privileged component that can only be kept current from the host is a
standing web-operability gap and a security-staleness liability.

### Why the existing machinery cannot simply be pointed at the sidecar

The app-recreation ladder does not generalize to the sidecar for four concrete
reasons rooted in the current code:

1. **Actor/target identity.** The install ladder *stops the target mid-operation*
   (`stop_container` → recreate → health-gate). The sidecar **is** the process
   running that ladder. Stopping itself kills the state machine driving the swap,
   and crash recovery (`run_once` replaying `state.json`) assumes the sidecar
   survives to resume. The app is a passive target; the sidecar is not.
2. **No enabled healthcheck.** `recreation.build_candidate_spec` *requires* an
   enabled healthcheck and raises `target_healthcheck_missing` otherwise; the
   pre-cutover gate calls `engine.wait_healthy`. The sidecar deliberately runs
   `healthcheck: disable: true` — it is an outbound poller, not an HTTP service —
   so the app's Docker health gate is structurally unavailable to it.
3. **Deliberately narrowed authority.** The executor's envelope is fixed to
   exactly one labeled target (`TARGET_LABEL` + `IMAGE_REF_LABEL`, name+label,
   never an arbitrary target). Widening it to a *second, itself-privileged*
   target expands the blast radius of the one container holding host-root-equivalent
   Docker access — the precise thing ADR-0024 narrowed against.
4. **Single-claimant invariant.** `StateStore.acquire` takes an exclusive
   `flock` on the private state volume; two updater processes cannot both drive.
   Any recreation scheme must keep "exactly one functioning updater, never zero,
   never two active claimants" true across every crash point.

## Constraints any solution must preserve

- **C1 — Never zero, never two claimants.** At every interruptible point there is
  exactly one functioning updater (old *or* new), enforced by the state flock and
  the compare-and-set lease, never a gap with none and never two drivers.
- **C2 — No new app authority.** The public web process must never gain Docker
  access (ADR-0024's central boundary).
- **C3 — Web-operable, button-not-terminal.** Refresh must not require a host
  shell on a `:stable` deployment.
- **C4 — Plain-Compose portability.** No systemd, host cron, Swarm, or
  orchestrator-specific auto-update contract (ADR-0003).
- **C5 — No interruption of an in-flight operation.** A sidecar refresh must not
  interleave with an app update, drain lease, or rollback, and must not wedge the
  coordinator phase machine (PR #346).
- **C6 — Honest recovery when refresh itself fails.** A failed sidecar refresh
  must leave a working updater and a *visible, retryable* state, never a silent
  `failed` or a terminal-only escape (north star #3).

## Options considered

### (a) Successor-spawn self-replacement *(recommended)*

The sidecar, only while no operation is in flight, pulls the promoted image,
creates a **successor** updater container from the new bytes, hands off the shared
state volume, and exits; the successor removes the predecessor. This is the
Watchtower/`docker-compose` self-update pattern, adapted to our coordinator.

- *C1:* Enforced by the existing `flock`. The predecessor releases any lease,
  stops its lease-keeper, then creates the successor; the successor blocks on the
  state lock until the predecessor exits, so only one drives at a time. The
  predecessor is removed only after the successor proves liveness.
- *Bootstrap paradox — real but bounded.* The container replaces itself, so the
  "healthy" gate cannot be Docker's healthcheck (constraint from §2). Substitute a
  **coordinator-observed liveness gate**: the successor writes a fresh heartbeat
  (`touch_updater`, the same signal the app already treats as "updater available"
  when younger than 45s); the predecessor confirms that heartbeat *from the new
  build id* before removing itself. This reuses machinery that already exists
  instead of inventing a health protocol.
- *Compose desired-state convergence — the subtle win.* Docker forbids two
  containers named `plex-manager-updater`, so the successor is created under a
  temporary name and promoted with the same create→stop→rename dance the app
  recreation already implements. Because **both** services track the *same moving
  tag*, a later `docker compose up -d` re-resolves `PLEX_MANAGER_IMAGE` to the new
  digest the sidecar already runs — it converges, it does **not** regress to old
  bytes. (This is why option (d) fails and (a) does not: the moving tag makes
  Compose agree with the self-swap after the fact.)
- *C2:* No app authority added; the sidecar remains the sole socket holder.
- *C5:* Self-refresh is gated to `state.json`-empty (no install/rollback pending)
  and takes no maintenance lease that could block app work; it never rewrites a
  coordinator phase, so it cannot create the unknown-phase wedge #346 guards.
- *Authority cost (the real price).* The executor's allowlist must widen from
  "one target" to "one target **plus self**." Mitigation: the successor/predecessor
  operations are constrained to containers bearing the updater's *own* role label
  + this operation id and the single known updater container name — still no
  arbitrary target, but the socket-holder can now create/stop/remove a second
  (privileged) container. This is the honest downside and must be disclosed like
  ADR-0024 disclosed the socket mount.
- *Rollback / C6:* The predecessor survives until it confirms successor liveness
  (mirrors the app's "old stays until new is healthy"). If the successor never
  heartbeats, the predecessor keeps running the old-but-working updater and
  surfaces a visible `updater_refresh_failed` status. If both die at the seam,
  `restart: unless-stopped` brings one back from the shared volume; the coordinator
  never wedges because self-refresh touches no phase.

### (b) Main app recreates the sidecar

Invert the relationship: the coordinator process recreates the sidecar.

- **Violates C2 outright.** The app has no Docker socket by design; giving it one
  so it can recreate the sidecar means a compromise of the public web process
  crosses straight into Docker-host control — the exact attack ADR-0024 rejected.
- Deadlock/wedge risk: the app is the coordinator; recreating the sidecar while
  the sidecar holds a lease is the phase-wedge class PR #346 hardened against.
- **Rejected.** It trades a staleness gap for the boundary ADR-0024 exists to
  hold.

### (c) Transient third actor / one-shot recreation container

A distinct short-lived container performs the swap, so actor ≠ target at the
moment of destruction, dissolving the "stop kills my own driver" paradox.

- Genuinely removes the bootstrap paradox and could keep a live supervisor.
- But the one-shot needs the Docker socket too — a **second privileged surface**,
  permanently (if it polls, it is just a second sidecar) or as a fragile
  socket-lent one-shot with its *own* crash-recovery ladder and its own
  allowlist. And something must *trigger* it web-operably without a host actor,
  which lands back on the sidecar spawning it — i.e. option (a) with an extra
  moving part.
- **Rejected as strictly more surface than (a)** for the same guarantees. Its one
  advantage (supervisor stays alive) is recoverable inside (a) by keeping the
  predecessor alive until successor liveness is confirmed.

### (d) Periodic self-exit + restart-policy pull

Have the sidecar exit cleanly on a timer and rely on the restart policy to pick up
new bytes.

- **Infeasible on the committed substrate.** Docker's `restart:` policy restarts
  the *same image ID* and never re-pulls; Compose `pull_policy` applies at
  `up`/`create`, not on restart. Bare self-exit reboots the identical stale image.
  Making "restart ⇒ new image" real requires Swarm, Podman auto-update labels, or a
  host `docker compose pull` timer — each breaks C4 (portability) and/or C3
  (web-operability).
- **Rejected as non-functional** on plain Compose across Linux/NAS/Desktop.

### (e) Documented operator action only (status quo, formalized)

Keep recreation a host command and make the docs/UI louder about it.

- **Violates C3 and north stars #1/#2 as a *sole* answer:** refresh stays a
  terminal command on the install host.
- *But* one component of it is worth keeping regardless of mechanism: the app can
  already detect the skew (its own build id vs. the sidecar heartbeat's build id)
  and **surface it honestly** (north star #3). Detect-and-surface is adopted below
  as a companion + pre-freeze interim, not as the resolution.

## Decision (recommended)

Adopt **option (a), successor-spawn self-replacement**, with these load-bearing
refinements: a coordinator-heartbeat liveness gate in place of the Docker
healthcheck; the create→temp-name→stop→rename-to-canonical dance so Compose
converges on the moving tag rather than regressing; `flock` + lease as the
single-claimant guarantee; a self-only allowlist widening (own role label + this
operation id + the one known updater container name, never an arbitrary target);
and a self-refresh that is gated to no-operation-in-flight and touches **no**
coordinator phase, so it cannot wedge the machine PR #346 hardened.

Reject (b) (breaks the app/Docker boundary), (d) (non-functional on plain
Compose), and (c) (more privileged surface than (a) for the same guarantee).
Reject (e) as a complete answer, but **adopt its detect-and-surface half** as a
required companion and a safe pre-freeze interim.

When self-refresh cannot complete (successor never heartbeats, or both die at the
seam), recovery is: the old-but-working predecessor keeps running and the app
shows a visible, retryable `updater_refresh_failed` state. Because self-refresh
never rewrites a phase, it cannot by itself produce the unknown-phase wedge; where
a *cross-release phase-vocabulary change* is the underlying fault, the in-app
recovery action from **#354** is the web-operable exit. The shape of that action
and this successor handoff must be cross-checked so they do not fight (per #354's
design note); this ADR references it generically rather than fixing its endpoint.

## Consequences

- The one privileged container keeps itself current from the browser; the
  standing web-operability gap and updater-side security-staleness liability
  close.
- The updater's authority envelope widens from one target to "one target + self."
  This is a real increase in the socket-holder's blast radius and must be
  disclosed in the Compose profile and operator docs exactly as ADR-0024 disclosed
  the socket mount.
- A new self-recreation ladder acquires its own crash-recovery states and a new
  liveness gate distinct from the app's Docker health gate; both need dedicated
  tests (successor promotion, predecessor survival on failed successor, both-die
  seam, no-two-claimants under flock, no-phase-wrote invariant).
- The `docker-compose.yml` `#299` limitation note is replaced by the automated
  path; the manual `up -d updater` command remains a documented fallback, not the
  primary route.
- Detect-and-surface (the adopted half of (e)) gives an honest interim before the
  full ladder ships and remains useful afterward as the failure surface.

## Implementation scope

- **Full option (a): Large.** A second recreation ladder with its own durable
  stages and crash reconciliation, a coordinator-heartbeat liveness gate, the
  self-only allowlist widening with its guard tests, and the Compose rename
  convergence — plus interaction tests against the #346 phase guard and the #354
  recovery action. This is new privileged design surface and should **not** be
  rushed to land before the **Jul 24** freeze.
- **Detect-and-surface interim (companion): Small–Medium.** Compare app build id
  to the sidecar heartbeat build id, expose a truthful "updater running older
  image" status, and keep the documented one-command refresh. Safe to land before
  the freeze; it adds no privileged authority.

**Recommendation for the freeze:** accept this ADR as `Proposed` now, ship the
Small–Medium detect-and-surface interim before Jul 24 if desired, and schedule the
Large successor-spawn ladder for the next window after the canary has exercised a
cross-release phase-vocabulary downgrade (the #354 scenario) so the two designs
land reconciled.

## Alternatives considered

Summarized above and rejected: **(b)** main-app recreation (breaks ADR-0024's
app/Docker boundary), **(c)** transient third actor (more privileged surface than
(a) for the same guarantee), **(d)** self-exit + restart-policy pull
(non-functional on plain Compose — restart never re-pulls), and **(e)** documented
operator action as the sole answer (violates north stars #1/#2; its
detect-and-surface half is adopted as a companion).

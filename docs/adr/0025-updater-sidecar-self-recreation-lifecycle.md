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
- **C7 — Sidecar↔app version-skew tolerance (internal API forward
  compatibility).** Any self-refresh advances the sidecar *ahead of* the app in
  real states: a sidecar-only fix release, an app update that failed and rolled
  back to N-1 while the sidecar already runs N+1, or a refresh racing the app's
  own update. The internal updater API today rejects anything it does not
  literally know: request models are fixed-literal with `extra="forbid"`
  (`UpdateHeartbeatRequest` accepts exactly `phase: "checking"` +
  `action_generation`, nothing more), so any new field, phase value, or
  operation from a newer sidecar is rejected outright by an older app.
  **Prerequisite:** before self-refresh ships, the internal updater API must be
  made forward-compatible under one explicit rule — either (i) *expand-first*,
  symmetric to ADR-0024's N/N-1 schema rule: a newer sidecar sends only what the
  oldest-supported app accepts, and every new field/phase/operation lands
  app-side (accepted, and persisted or ignored) at least one release before any
  sidecar emits it; or (ii) a *capability handshake* in which the sidecar first
  asks what the app accepts and degrades to the older vocabulary. A mechanism
  that skips this prerequisite strands a refreshed sidecar unable to even report
  liveness to the app it serves.

## Options considered

### (a) Successor-spawn self-replacement *(recommended)*

The sidecar, only while no operation is in flight, pulls the promoted image and
creates a **successor** updater container from the new bytes; the predecessor
**survives until it confirms the successor is alive**, then stops itself, and the
successor — once it holds the state lock — removes the predecessor and takes over
the canonical identity. This is the Watchtower/`docker-compose` self-update
pattern, adapted to our coordinator. The handoff is one ordered sequence:

1. Gate: `state.json` empty (no install/rollback pending) and no lease held.
2. The predecessor pulls the promoted image, writes a durable **self-refresh
   record** to the shared state volume (operation id, **its own container id**,
   and the temporary successor name), and creates + starts the successor under
   that temporary name.
3. The successor starts in a new **successor-bootstrap mode**: it emits its
   coordinator heartbeat (`touch_updater`, carrying its build identity — a new
   protocol field, see below) **before** taking the state lock, then wait-loops
   on the `flock`. This inverts today's startup order, in which the process
   acquires the state lock fail-fast before doing anything. The heartbeat is an
   HTTP write to the app's coordinator, **not** gated by the flock, so the
   successor can prove liveness while the predecessor still holds the lock.
   Until it owns the lock, the successor performs *no* Docker mutation and no
   coordination operation beyond that liveness ping.
4. The predecessor confirms, within a bounded window, a fresh heartbeat carrying
   the **new build id**. On timeout it removes the temp-named successor,
   surfaces a visible `updater_refresh_failed` state, and keeps running
   (old-but-working). On confirmation it stops itself **through the Engine API**
   — a user-initiated stop, so `restart: unless-stopped` does not resurrect it —
   and exits, releasing the flock.
5. The successor acquires the flock, reconciles the self-refresh record, removes
   the stopped predecessor (matched by the **container id recorded in step 2**,
   verified stopped — see the first-predecessor carve-out below), and renames
   itself to the canonical `plex-manager-updater` name.

- *C1:* **Never zero** — the predecessor stays fully functional until step 4's
  confirmed heartbeat, and a timeout leaves it running. **Never two claimants**
  — the existing `flock` bars the successor from driving any operation until the
  predecessor has exited; the step-3 liveness ping is not a claimant operation.
- *Bootstrap paradox — real but bounded.* The container replaces itself, so the
  "healthy" gate cannot be Docker's healthcheck (constraint from §2). Steps 3–4
  substitute a **coordinator-observed liveness gate**: `touch_updater` is the
  same signal the app already treats as "updater available" when younger than
  45s. This reuses machinery that already exists instead of inventing a health
  protocol. (Implementation note: `StateStore.acquire` today is a fail-fast
  non-blocking flock; step 3's wait-loop is a retry around it, not a new locking
  primitive.)
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
  "one target" to "one target **plus self**." Mitigation: successor operations
  are constrained to containers bearing the updater's *own* role label + this
  operation id; the predecessor is matched only by the exact container id it
  recorded about itself (see the first-predecessor carve-out below) — still no
  arbitrary target, but the socket-holder can now create/stop/remove a second
  (privileged) container. This is the honest downside and must be disclosed like
  ADR-0024 disclosed the socket mount.
- *Rollback / C6:* Step 4 mirrors the app's "old stays until new is healthy": if
  the successor never heartbeats, the predecessor keeps running the
  old-but-working updater and surfaces a visible `updater_refresh_failed`
  status. If both die at the seam, the temp-named successor still carries
  `restart: unless-stopped` (only the predecessor's step-4 stop is
  user-initiated), so Docker restarts it onto the shared volume and it
  reconciles from the self-refresh record; the coordinator never wedges because
  self-refresh touches no phase.
- *Named crash window — orphaned temp successor.* Between step 2
  (successor created under the temporary name) and step 5 (rename to canonical),
  a crash can strand a temp-named updater container. The durable self-refresh
  record written **before** the create (step 2) is the recovery anchor — exactly
  the role the app ladder's durable `candidate_renamed`/`old_renamed` stages
  play. On restart, whichever updater reconciles first consults the record: a
  predecessor that never confirmed the heartbeat removes the orphan and surfaces
  `updater_refresh_failed`; a successor that finds the predecessor already
  stopped completes step 5 (remove + rename). Reconciliation is keyed on the
  record's operation id (successor side) and recorded container id (predecessor
  side), so a half-renamed or duplicate-named leftover is never guessed at —
  unmatched containers are refused, not adopted.

#### New protocol and recovery surface (named work items)

Option (a) is not implementable on today's wire contract and storage alone.
Three additions are explicit work items, each subject to constraint C7's
forward-compatibility rule:

1. **Successor build identity in the heartbeat (protocol + storage).** Step 4's
   confirmation — and the detect-and-surface companion — require the app to know
   *which build* is heartbeating. Today `UpdateHeartbeatRequest` carries only
   `phase` + `action_generation`, and `touch_updater` persists only
   `updater_last_seen_at`/`phase`; no build identity exists anywhere in the
   authenticated heartbeat or coordinator storage. The work item: add a sidecar
   build identity to the heartbeat and persist it in coordinator status storage
   (alongside `updater_last_seen_at`), exposed on the status surface. Per C7,
   the app-side model must accept and store the field **at least one release
   before** any sidecar sends it (`extra="forbid"` would otherwise reject the
   newer sidecar's first ping) — which is exactly why the detect-and-surface
   interim doubles as the expand step of this protocol change.
2. **First-predecessor carve-out (allowlist).** The Compose-created `updater`
   service carries **no** role label, and an operation id exists only at
   runtime, so a label-only allowlist could never match the very first stale
   sidecar for stop/remove — existing installs would still need a terminal for
   refresh number one. The carve-out: the predecessor side of the swap is never
   matched by label at all. The predecessor *stops itself* (step 4), and the
   successor removes it by the **exact immutable container id the predecessor
   recorded about itself** in the step-2 self-refresh record, cross-checked
   against the canonical container name and the configured image repository
   lineage, and verified stopped before removal. Security rationale: a
   self-recorded container id persisted in the private state volume is a
   *narrower* authority than any label or name match — it can only ever
   designate the one container that wrote it. Successors are created *with*
   role/operation labels (step 2), so the label discipline holds from the first
   refresh onward; the carve-out applies only to predecessors, permanently,
   because every predecessor was once a container whose labels this design
   cannot assume.
3. **Durable refresh-status record (failure persistence).** The coordinator
   records only check/install outcomes today, and a surviving predecessor's
   regular heartbeats would make the updater look perfectly healthy after a
   failed refresh — a silent failure (north star #3). `updater_refresh_failed`
   therefore needs its own persistence, **distinct from coordinator phase** to
   preserve the no-phase-writes invariant: a small app-side refresh-status
   record (at ADR altitude: last refresh outcome, bounded detail code,
   from/to build identities, timestamp) written through a dedicated
   authenticated internal endpoint (again under C7's expand-first rule), read
   by the admin status surface, and mirrored crash-consistently by the
   sidecar's own self-refresh record in the state volume. Ordinary heartbeats
   never clear it; it is cleared only by a later successful refresh or an
   explicit operator acknowledgement.

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
- *But* one component of it is worth keeping regardless of mechanism: the app is
  the natural place to detect the skew (its own build id vs. the sidecar's) and
  **surface it honestly** (north star #3) — once the heartbeat carries a build
  identity, which is itself a named work item under option (a). Detect-and-surface
  is adopted below as a companion + pre-freeze interim, not as the resolution.

## Decision (recommended)

Adopt **option (a), successor-spawn self-replacement**, with these load-bearing
refinements: the **survive-to-confirm handoff ordering** (steps 1–5 above — the
predecessor outlives the successor's first confirmed heartbeat and only then
stops itself; viable precisely because `touch_updater` is an HTTP write the
successor can emit before it holds the state flock); a coordinator-heartbeat
liveness gate in place of the Docker healthcheck; the
create→temp-name→stop→rename-to-canonical dance so Compose converges on the
moving tag rather than regressing; `flock` + lease as the single-claimant
guarantee; a self-only allowlist widening (labeled successors plus the
id-pinned first-predecessor carve-out, never an arbitrary target); and a
self-refresh that is gated to no-operation-in-flight and touches **no**
coordinator phase, so it cannot wedge the machine PR #346 hardened.

Adoption carries the explicit prerequisites named in option (a): the internal
updater API must satisfy C7's forward-compatibility rule before any refreshed
sidecar exists, the heartbeat must gain a persisted successor build identity,
and refresh failure needs its own durable status record separate from
coordinator phase. None of these exist on today's wire contract.

Reject (b) (breaks the app/Docker boundary), (d) (non-functional on plain
Compose), and (c) (more privileged surface than (a) for the same guarantee).
Reject (e) as a complete answer, but **adopt its detect-and-surface half** as a
required companion and a safe pre-freeze interim.

When self-refresh cannot complete, recovery is: on a successor that never
heartbeats, the old-but-working predecessor keeps running and the app shows a
visible, retryable `updater_refresh_failed` state (persisted in the durable
refresh-status record, not in coordinator phase); on a crash inside the
orphaned-temp-successor window, the survivor reconciles from the durable
self-refresh record as specified in option (a). Because self-refresh
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
- The internal updater API acquires C7's forward-compatibility rule as a
  release-process requirement for every release whose sidecar may outpace the
  app — exactly as ADR-0024's N/N-1 migration rule became one for schemas.
- A new self-recreation ladder acquires its own crash-recovery states and a new
  liveness gate distinct from the app's Docker health gate; both need dedicated
  tests (successor promotion, predecessor survival on failed successor, the
  orphaned-temp-successor window between create and rename, both-die seam,
  no-two-claimants under flock, the id-pinned first-predecessor carve-out
  refusing unmatched containers, no-phase-wrote invariant).
- The `docker-compose.yml` `#299` limitation note is replaced by the automated
  path; the manual `up -d updater` command remains a documented fallback, not the
  primary route.
- Detect-and-surface (the adopted half of (e)) gives an honest interim before the
  full ladder ships and remains useful afterward as the failure surface.

## Implementation scope

- **Full option (a): Large.** A second recreation ladder with its own durable
  stages and crash reconciliation, a coordinator-heartbeat liveness gate, the
  self-only allowlist widening (labeled successors + the id-pinned
  first-predecessor carve-out) with its guard tests, the C7
  forward-compatibility rule on the internal updater API, the durable
  refresh-status record, and the Compose rename convergence — plus interaction
  tests against the #346 phase guard and the #354 recovery action. This is new
  privileged design surface and should **not** be rushed to land before the
  **Jul 24** freeze.
- **Detect-and-surface interim (companion): Small–Medium.** This interim *is*
  the expand step C7 requires: add the sidecar build identity to the
  authenticated heartbeat and coordinator status storage (app-side accept +
  persist first, sidecar sending it thereafter), compare it to the app's own
  build id, expose a truthful "updater running older image" status, and keep
  the documented one-command refresh. Safe to land before the freeze; it adds
  no privileged authority and pre-pays the protocol change the full ladder
  depends on.

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

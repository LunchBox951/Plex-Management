# ADR-0023: First-party container updater with app-owned policy

- **Status:** Accepted
- **Date:** 2026-07-12
- **Resolves:** [issue #200](https://github.com/LunchBox951/Plex-Management/issues/200)
- **Context builds on:** [ADR-0003](0003-docker-ghcr-packaging.md) (Docker/GHCR
  packaging), [ADR-0004](0004-edge-stable-release-channels.md) (tag-controlled
  channels), [ADR-0005](0005-zero-terminal-web-operability.md) (web operation),
  and [ADR-0007](0007-sqlite-alembic-migrations.md) (forward migrations).

## Context

Plex Manager publishes moving `:edge` and `:stable` tags, but an operator still
has to pull and recreate the container. A generic updater can notice a new
image, but it cannot safely answer product-specific questions: whether the
configured maintenance window is open, whether a grab/import/correction is at a
safe boundary, or what result should be shown in the admin UI.

Giving the application process the Docker socket would make those decisions
easy to execute, but it would also give the public web process host-level Docker
authority. Running a host timer avoids that boundary but breaks the project's
web-operability requirement and is not portable across Docker Desktop, NAS
appliances, and ordinary Linux Compose installations.

## Decision

Ship an **opt-in, first-party updater sidecar** in the Compose application. Plex
Manager owns policy, scheduling, idle/drain coordination, and operator-visible
state. The sidecar owns image inspection/pull and container replacement.

### Authority and authentication boundary

- Only the updater mounts the Docker socket. The Plex Manager application never
  receives Docker API access.
- The updater has no published port and calls narrowly scoped internal
  coordination endpoints over the private Compose network.
- Those endpoints require a dedicated, random bearer secret mounted into both
  containers as a Compose secret. Browser sessions and the recovery API key do
  not authenticate the internal surface, and the updater secret does not
  authenticate the public API.
- The executor is fixed to the configured Plex Manager container name and image
  repository/tag. It additionally requires the project's opt-in management
  label. Requests cannot supply an arbitrary image, container, or Docker action.
- This is still a privileged deployment choice: mounting the Docker socket gives
  the updater effective control over the Docker host. The feature is disabled
  by default and the Compose profile and operator documentation disclose that
  authority explicitly.

### Policy and coordination

The persisted default policy is disabled, every weekday, 03:00–05:00 in an
explicit IANA timezone, and idle-only. Start and end must differ and at least one
weekday must be selected. An overnight window belongs to the weekday on which
it starts. Daylight-saving gaps and folds are resolved by the scheduler in the
configured local timezone rather than by fixed UTC offsets.

Idle means no Plex Manager critical mutation is active: grab handoff,
import/move/scan, correction/purge, eviction, or an administrative write.
Playback and qBittorrent transfer activity do not block. When an update is
ready, the sidecar obtains a short database-backed maintenance lease. Existing
critical work drains; new requests may be accepted but their critical work stays
queued until the lease is released or expires. Claims, renewals, release, and
outcome acknowledgement are compare-and-set operations. Eligibility fails
closed when state is uncertain, another claim exists, or the coordinator cannot
be reached.

### Image and replacement behavior

`PLEX_MANAGER_IMAGE` remains the only channel selector. The updater resolves the
configured tag to an immutable digest, and an unchanged digest is a no-op. For a
changed digest it persists each replacement stage outside the target container,
pulls the new image, recreates only the labeled Plex Manager container while
preserving its effective mounts, environment, ports, networks, labels, restart
policy, and healthcheck, and waits for health before acknowledging success.

The previous container and image remain available until the replacement is
healthy. Failed startup restores the previous image and effective container
configuration. Interrupted stages are reconciled idempotently from persistent
updater state on restart. Logs and API errors expose only bounded stage/error
codes, never credentials, raw registry responses, environment contents, or
secret-bearing URLs.

### Migration compatibility and rollback

Container rollback does **not** run `alembic downgrade`; data migrations and
schema contraction cannot be made reliably reversible after a newer process has
served traffic. Therefore every release eligible for automatic update must obey
this compatibility rule:

> After release N applies its migrations, the immediately previous release
> N-1 must still be able to start and provide its supported behavior against the
> resulting database.

The rollback executor implements that rule by creating a clone from the retained
N-1 image and effective container configuration, then overriding the clone's
entrypoint to start the N-1 application module directly. It deliberately bypasses
N-1's normal startup migration command: after N has stamped its revision, an
older Alembic graph may reject that unknown revision before the otherwise
compatible N-1 application can serve. The rollback clone must still pass the
retained healthcheck. This bypass neither reverses nor skips N's already-applied
forward migration; it relies on the post-migration schema satisfying the N/N-1
application compatibility rule.

Schema changes on the auto-update train are expand-first and tolerant of unknown
columns/rows. A destructive rename, type narrowing, constraint tightening, or
column/table removal must be split across releases: expand and dual-read/write,
then migrate/backfill and stop old writes, and only contract after the rollback
horizon has passed. If that guarantee cannot be met, the release must stay off
moving tags consumed by automatic updates and ship only with documented
pinned/manual upgrade instructions. Publishing to such a tag is the release
process's assertion that the N/N-1 rule holds; this first version does not define
or enforce a per-image compatibility flag in the updater.

## Consequences

- Operators can control updates completely from the admin UI without granting
  the public application process Docker authority.
- Standard Compose remains the deployment unit across Linux, NAS, and Docker
  Desktop; no systemd or host cron contract is required.
- The updater is intentionally small but security-sensitive. Its allowlists,
  internal authentication, Docker recreation fidelity, health gate, and
  interrupted-stage recovery require dedicated tests.
- Automatic update rollback restores application bytes and container
  configuration, not old database bytes. The N/N-1 migration compatibility rule
  becomes a release-process requirement for every moving automatic-update tag.
- Opting in adds a Docker-socket-bearing container. Operators who do not accept
  that authority can leave the profile disabled and update with normal Compose
  commands.

## Alternatives considered

- **Watchtower.** Mature and convenient, but it cannot implement Plex Manager's
  persisted schedule, drain lease, honest product status, or migration-aware
  outcome protocol without recreating policy out of band.
- **Host systemd/cron running `docker compose pull && up -d`.** Keeps the socket
  out of a sidecar, but is not web-operable and excludes common NAS/Desktop
  deployments.
- **Docker socket in Plex Manager.** Rejected because a compromise of the public
  web process would immediately cross into Docker-host control.
- **Update button only.** Useful as a manual action, but does not satisfy the
  requested scheduled, idle-only behavior. The UI still exposes check-now and
  update-when-ready actions on top of the same coordinator.

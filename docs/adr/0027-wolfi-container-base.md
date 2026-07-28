# ADR-0027: Use a digest-pinned Wolfi/glibc container base

**Status:** Accepted
**Date:** 2026-07-28
**Qualifies:** ADR-0003 (Docker/GHCR packaging)

## Context

The Debian `python:3.14-slim` runtime carried a large base-OS vulnerability
surface in packages the application does not use directly and cannot remove
without leaving Debian's supported package composition. Suppressing unfixed OS
findings in SARIF keeps the Security board actionable, but does not remove that
surface.

A replacement must preserve the existing production contract: Python 3.14,
glibc-compatible extension wheels, `/bin/sh` for the entrypoint, `ffprobe` for
ADR-0017 media validation, numeric UID/GID `10001:10001` for the app, and the
same image operating as UID/GID `0:0` for the ADR-0024 updater sidecar. It must
also preserve ADR-0004's promotion identity: stable is a re-tag of the tested
GHCR image digest, never a rebuild.

## Decision

Use Wolfi's glibc-based `wolfi-base` for both Python stages, pinned to:

```text
cgr.dev/chainguard/wolfi-base:latest@sha256:003627df3c1e1bba0c4116afcddb314aca9594ee2328c7e876a8081a6c988b2e
```

Install exact APK versions rather than following repository latest implicitly:

```text
python-3.14=3.14.6-r4
py3.14-pip=26.1.2-r1       # builder only
ffmpeg-8.1=8.1.2-r2        # runtime; supplies ffprobe
```

The application runs as numeric `10001:10001`; no passwd entry or `shadow`
package is needed. The Compose updater retains its explicit `0:0` override.
The shell entrypoint and an exec-form Python healthcheck remain in the image.
Container CI exercises the default runtime, root updater import, and normal
boot-to-healthy path before scanning.

## Measured evidence

Both images were built for `linux/amd64` from baseline commit
`989a4131ea2bc45fbd2d0acb1b4f461d4e7798f1` with `--pull --no-cache`. The
Debian source resolved to
`python@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6`;
the Wolfi source is the digest above. Trivy 0.70.0 scanned both local images
with one downloaded DB snapshot (`UpdatedAt` `2026-07-28T13:20:17.81613423Z`),
`--skip-db-update --pkg-types os --ignore-unfixed=false`. Counts deduplicate
identical `(target, package, vulnerability ID)` occurrences.

| Metric | Debian baseline | Wolfi candidate |
|---|---:|---:|
| Image size (bytes) | 256,857,285 | 131,640,029 |
| Total OS findings | 535 | 0 |
| Critical | 8 | 0 |
| High | 59 | 0 |
| Critical + high | 67 | 0 |
| Fixable critical + high | 0 | 0 |
| Unfixed critical + high | 67 | 0 |

The candidate is 51.2% of the Debian image size (a 125,217,256-byte reduction).
Its total OS findings and critical/high findings are each 0% of baseline, below
the required 50% ceilings, and it adds no fixable critical/high occurrence.
These are point-in-time scanner results, not a guarantee that Wolfi will remain
finding-free; CI and the scheduled rescan remain authoritative over time.

## Alternatives considered

- **Direct Chainguard Python image:** deferred because the freely accessible
  image/tag contract did not provide the independently verifiable, versioned
  Python and ffmpeg package composition required here. `wolfi-base` plus exact
  APK pins makes every selected runtime package explicit.
- **Alpine Python:** rejected because musl changes the extension-wheel ABI and
  creates unnecessary compatibility and source-build risk for cryptography,
  asyncpg, psycopg2, uvloop, watchfiles, and pydantic-core.
- **Distroless:** rejected because its available Python line would reverse the
  ADR-0002 Python 3.14 choice and it does not compose the required shell and
  ffprobe runtime contract.
- **Remain on Debian slim:** rejected because it retains the measured unused OS
  vulnerability surface and larger image while Wolfi passes the same runtime
  contracts.

## Consequences

- Base and APK updates are deliberate pin changes reviewed through the container
  runtime smokes and full OS scan; repository availability of every exact pin is
  a build prerequisite and failures are not worked around by loosening pins.
- The project depends on Chainguard's registry and Wolfi APK repository for base
  and package provenance. Digest pinning prevents a moving `latest` tag from
  silently changing the selected base.
- The final application image remains the release unit. ADR-0004 is unchanged:
  CI builds and scans an immutable GHCR digest, canary validates those bytes, and
  stable promotion re-tags that same digest without rebuilding or re-resolving
  Wolfi packages.
- ADR-0002's Python 3.14 stack, ADR-0017's ffprobe validation, and ADR-0024's
  root updater role remain intact.

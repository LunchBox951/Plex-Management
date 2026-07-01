# Security Policy

## Supported versions

Plex Manager ships on two channels (see
[ADR-0004](docs/adr/0004-edge-stable-release-channels.md)):

| Channel | Image tag | Support |
|---|---|---|
| Stable | `:stable`, `:x.y.z` | Security fixes |
| Edge | `:edge` | Pre-release; fixes land here first |

The project is pre-1.0; only the latest build on each channel is supported.

## Reporting a vulnerability

**Do not open a public issue for security problems.** Instead use GitHub's
**private vulnerability reporting** (Security → "Report a vulnerability"), or
email **95397613+LunchBox951@users.noreply.github.com **.

Please include reproduction steps and the affected version/tag. You can expect an
acknowledgement within a few days. Because this is a self-hosted home application,
there is no bug-bounty program.

## How secrets are handled

- Service credentials (Plex token, TMDB / Prowlarr / qBittorrent keys) are entered
  through the in-app setup wizard and **stored encrypted at rest**, never in the
  image or in `.env`.
- First-run setup is guarded by `PLEX_MANAGER_SETUP_TOKEN` in the stock Docker
  Compose deployment, and the default published host bind is loopback-only. Keep
  that token out of issue reports, logs, screenshots, and public compose examples.
- Secrets **must never be written to logs** — enforced in review today, and to be
  backed by a logging redaction filter and a test once the secrets code lands (a
  regression carried over from a prototype lesson).

## Automated security checks (CI)

| Check | Tool |
|---|---|
| Static analysis (SAST) | CodeQL |
| Dependency vulnerabilities | `pip-audit` (runtime deps) + Dependabot alerts (incl. dev) |
| Python security lints | `ruff` (bandit `S` rules) |
| Secret scanning | gitleaks (CI) + GitHub secret scanning (repo setting) |
| Container image CVEs | Trivy — scans the built image on every PR and on push to `main`; report-only, all severities (the Security tab is the honest tally) |
| Dependency / Action / base-image updates | Dependabot |

GitHub Actions are version-pinned and kept current by Dependabot. Pinning Actions
to commit SHAs remains a planned hardening step.

## Repository hardening checklist

These are GitHub **settings**, not files, so they are tracked here rather than
committed. Current status:

- [x] **Secret scanning** and **push protection** enabled.
- [x] **Private vulnerability reporting** enabled.
- [x] **Dependabot alerts** and security updates enabled.
- [x] CodeQL **advanced** setup is configured via
      `.github/workflows/codeql.yml` (runs on every PR to `main` and on push) —
      do **not** also enable CodeQL *default* setup (the two conflict and default
      would disable the committed workflow).
- [x] `main` is protected: a PR is required; the `quality`, `tests-py314`,
      `frontend`, `analyze (python)`, `analyze (javascript-typescript)`,
      `secret-scan`, `dependency-audit`, and `build` (container) checks must
      pass; branches must be up to date; and force-pushes and deletion are
      blocked. Admins are **not** forced through the gate, so the solo maintainer
      can still self-merge and hotfix.
- [ ] Restrict GHCR package visibility/permissions as desired.
- [ ] (Planned) pin Actions to commit SHAs — Dependabot keeps the version tags
      current in the meantime.

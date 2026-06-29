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
**private vulnerability reporting** (Security → "Report a vulnerability") once the
repository exists, or email **95397613+LunchBox951@users.noreply.github.com **.

Please include reproduction steps and the affected version/tag. You can expect an
acknowledgement within a few days. Because this is a self-hosted home application,
there is no bug-bounty program.

## How secrets are handled

- Service credentials (Plex token, TMDB / Prowlarr / qBittorrent keys) are entered
  through the in-app setup wizard and **will be stored encrypted at rest**, never
  in the image or in `.env`. (The wizard + encryption land with v1; the foundation
  already enforces the "never in the image or `.env`" half — `.env` is git- and
  docker-ignored.)
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
| Container image CVEs | Trivy (report-only, post-merge until the base image is clean) |
| Dependency / Action / base-image updates | Dependabot |

GitHub Actions are version-pinned and kept current by Dependabot. Pinning Actions
to commit SHAs is a planned hardening step before the repository is made public.

## Repository hardening checklist (enable after creating the GitHub repo)

These are GitHub **settings**, not files, so they are tracked here rather than
committed:

- [ ] Enable **secret scanning** and **push protection**.
- [ ] Enable **private vulnerability reporting**.
- [ ] Enable **Dependabot alerts** and security updates.
- [ ] CodeQL **advanced** setup is already configured via
      `.github/workflows/codeql.yml` — do **not** also enable CodeQL *default*
      setup (the two conflict and default would disable the committed workflow).
- [ ] Protect `main`: require PRs, require CI + CodeQL status checks to pass,
      disallow force-pushes.
- [ ] Restrict GHCR package visibility/permissions as desired.
- [ ] (Planned, before going public) pin Actions to commit SHAs.

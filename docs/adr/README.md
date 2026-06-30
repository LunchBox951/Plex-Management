# Architecture Decision Records

Each ADR captures **one** significant, hard-to-reverse decision: its context, the
choice, the consequences, and the alternatives we rejected. ADRs are immutable
once accepted — to change a decision, add a new ADR that supersedes the old one.

| # | Decision | Status |
|---|---|---|
| [0001](0001-integrated-app-borrowed-brains.md) | Integrated app with *borrowed brains* (Option C) | Accepted |
| [0002](0002-python-typed-stack.md) | Strictly-typed Python stack | Accepted |
| [0003](0003-docker-ghcr-packaging.md) | Ship as a Docker image via GHCR | Accepted |
| [0004](0004-edge-stable-release-channels.md) | `:edge` / `:stable` channels by tag promotion | Accepted |
| [0005](0005-zero-terminal-web-operability.md) | Zero-terminal, web-operable release deployment | Accepted |
| [0006](0006-download-client-port-qbittorrent.md) | `DownloadClient` port; qBittorrent as v1 adapter | Accepted |
| [0007](0007-sqlite-alembic-migrations.md) | SQLite + Alembic migrations from day one | Accepted |
| [0008](0008-release-parser-guessit.md) | `guessit` parses; the quality model stays ours | Accepted |

ADRs 0001–0007 were accepted on **2026-06-29** during the v2 brainstorming
session; 0008 during the first backend-alpha session. Full context:
[`docs/design/overview.md`](../design/overview.md).

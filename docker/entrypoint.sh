#!/usr/bin/env sh
# Apply database migrations before serving. Because the canary host runs :edge
# first (ADR-0004), any migration is exercised there before reaching :stable.
# Before upgrading, snapshot the DB + encryption key as one recovery unit when a
# migration is pending (ADR-0021) -- advisory, fail-loud, never bricks startup.
set -e

python -m plex_manager.db_backup
alembic upgrade head
exec python -m plex_manager

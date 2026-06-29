#!/usr/bin/env sh
# Apply database migrations before serving. Because the canary host runs :edge
# first (ADR-0004), any migration is exercised there before reaching :stable.
set -e

alembic upgrade head
exec python -m plex_manager

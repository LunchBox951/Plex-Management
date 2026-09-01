"""Plex Manager — self-hosted, unified media request & automation service for Plex."""

# Single source of truth for the app/package version: hatch reads this file
# directly (see [tool.hatch.version] in pyproject.toml), FastAPI surfaces it as
# OpenAPI `info.version` (web/app.py), events.current_build_id() falls back to
# it when no image build id is injected, and container.yml bakes it into the
# `org.opencontainers.image.version` OCI label that the promote gate checks.
# Bump it per CONTRIBUTING.md's release checklist (step 2) *before* the `:edge`
# build intended for promotion; CHANGELOG.md's release cut (step 1) may lag
# until promotion day.
__version__ = "1.1.0"

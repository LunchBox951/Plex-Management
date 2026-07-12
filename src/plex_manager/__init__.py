"""Plex Manager — self-hosted, unified media request & automation service for Plex."""

# Single source of truth for the app/package version: hatch reads this file
# directly (see [tool.hatch.version] in pyproject.toml), FastAPI surfaces it as
# OpenAPI `info.version` (web/app.py), and events.current_build_id() falls back
# to it when no image build id is injected. No release has been promoted yet
# (see CHANGELOG.md `[Unreleased]`), so this stays "0.0.0" until the maintainer
# bumps it as the first step of the release checklist in CONTRIBUTING.md — see
# ADR-0021.
__version__ = "0.0.0"

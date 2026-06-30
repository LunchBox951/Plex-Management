"""Thin async orchestration the web layer calls.

These services compose the adapters (TMDB / Prowlarr / qBittorrent), the
repositories, and the pure domain (decision engine, reconciler, state machine)
into the alpha "search -> grab -> reconcile" flow. They are deliberately small:
each is a handful of functions, adapter/DB-aware but holding no business rules of
their own (those live in ``domain/``). The web routers depend on these; the
services depend on ``adapters`` / ``domain`` / ``ports`` / ``repositories``.
"""

from __future__ import annotations

__all__: list[str] = []

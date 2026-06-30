"""SPA serving (spa.mount_spa) + the setup-guard's non-API pass-through."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from plex_manager.web import spa


def _build_static(root: Path) -> Path:
    """Create a minimal built-SPA layout (index.html + one hashed asset)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<!doctype html><title>SPA SHELL</title>", encoding="utf-8")
    assets = root / "assets"
    assets.mkdir()
    (assets / "index-abc123.js").write_text("console.log('app')", encoding="utf-8")
    return root


def _point_spa_at(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    resolved = root.resolve()
    monkeypatch.setattr(spa, "_STATIC_DIR", resolved)
    monkeypatch.setattr(spa, "_ASSETS_DIR", resolved / "assets")
    monkeypatch.setattr(spa, "_INDEX_FILE", resolved / "index.html")


async def _drive(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _ping() -> dict[str, bool]:
    return {"ok": True}


async def test_mount_spa_serves_shell_assets_and_client_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _point_spa_at(monkeypatch, _build_static(tmp_path / "static"))

    app = FastAPI()
    app.add_api_route("/api/v1/ping", _ping)
    spa.mount_spa(app)

    async with await _drive(app) as client:
        # Root + a client-side route both return the shell (deep links survive refresh).
        root = await client.get("/")
        assert root.status_code == 200
        assert "SPA SHELL" in root.text

        route = await client.get("/queue")
        assert route.status_code == 200
        assert "SPA SHELL" in route.text

        # Hashed assets are served from /assets.
        asset = await client.get("/assets/index-abc123.js")
        assert asset.status_code == 200
        assert "console.log" in asset.text

        # A real API route still wins over the catch-all...
        assert (await client.get("/api/v1/ping")).json() == {"ok": True}
        # ...and an UNMATCHED api path 404s honestly instead of returning the shell.
        unknown = await client.get("/api/v1/nope")
        assert unknown.status_code == 404
        assert "SPA SHELL" not in unknown.text


async def test_mount_spa_is_noop_when_not_built(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Point at a directory with no index.html -> nothing is mounted.
    _point_spa_at(monkeypatch, tmp_path / "empty")
    (tmp_path / "empty").mkdir()

    app = FastAPI()
    spa.mount_spa(app)
    assert spa.spa_is_built() is False

    async with await _drive(app) as client:
        assert (await client.get("/")).status_code == 404


async def test_ui_path_not_guarded_pre_init(client: httpx.AsyncClient) -> None:
    # The guard must let the SPA shell / client routes through pre-init so the
    # wizard can render; only the protected API gets the 409.
    response = await client.get("/setup")
    assert response.status_code != 409

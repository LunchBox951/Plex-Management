"""Smoke test for the runnable skeleton: the health endpoint responds."""

from __future__ import annotations

from fastapi.testclient import TestClient

from plex_manager.web.app import app


def test_health_returns_ok() -> None:
    # base_url="http://localhost": TestClient's own default host ("testserver")
    # is now rejected by TrustedHostMiddleware like any other untrusted Host,
    # so this smoke test drives it through the same trusted hostname the rest
    # of the web suite uses (see tests/web/conftest.py).
    client = TestClient(app, base_url="http://localhost")
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

"""Smoke test for the runnable skeleton: the health endpoint responds."""

from __future__ import annotations

from fastapi.testclient import TestClient

from plex_manager.web.app import app


def test_health_returns_ok() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

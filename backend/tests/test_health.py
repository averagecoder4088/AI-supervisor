"""Tests for health check endpoint."""

from fastapi.testclient import TestClient
from app.main import app
from app.config import get_settings


client = TestClient(app)


def test_health_check_returns_ok():
    """Verify that GET /health returns status 200 and expected payload."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "app_env" in data


def test_health_check_reflects_current_env():
    """Verify that GET /health reflects the configured app_env."""
    settings = get_settings()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["app_env"] == settings.app_env


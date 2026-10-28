from fastapi.testclient import TestClient

from app import main
from app.config import get_settings


def test_auth_precedes_dependency_access(monkeypatch):
    def forbidden():
        raise AssertionError("Dependencies must not be accessed")
    monkeypatch.setattr(main, "check_dependencies", forbidden)
    with TestClient(main.app) as client:
        for headers in ({}, {"X-API-Key": "bad"}):
            assert client.get("/api/v1/health", headers=headers).status_code == 401
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_health_success(monkeypatch):
    monkeypatch.setattr(main, "check_dependencies", lambda: {"database": "ok", "redis": "ok", "schema": "0001"})
    response = TestClient(main.app).get("/api/v1/health", headers={"X-API-Key": get_settings().internal_api_key.get_secret_value()})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_failure_is_redacted(monkeypatch):
    def fail():
        raise RuntimeError("secret password")
    monkeypatch.setattr(main, "check_dependencies", fail)
    response = TestClient(main.app).get("/api/v1/health", headers={"X-API-Key": get_settings().internal_api_key.get_secret_value()})
    assert response.status_code == 503
    assert "secret" not in response.text

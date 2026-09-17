from __future__ import annotations

from app.core.config import get_settings


def test_live(client):
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_all_checks(client):
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["reasons"] == []
    assert all(body["checks"].values())


def test_ready_reports_failure(client):
    settings = get_settings()
    original = settings.tts_concurrency
    settings.tts_concurrency = 0
    try:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["reasons"] == ["concurrency"]
    finally:
        settings.tts_concurrency = original

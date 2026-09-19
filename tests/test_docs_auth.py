from __future__ import annotations

import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.middleware import DocsAuthMiddleware


def _basic(password: str, username: str = "docs") -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _client(password: str) -> TestClient:
    settings = Settings(
        app_env="development",
        hmac_keys_json='{"k":"s"}',
        docs_password=password,
    )
    app = FastAPI(docs_url="/docs", openapi_url="/openapi.json")
    app.add_middleware(DocsAuthMiddleware, settings=settings)
    return TestClient(app)


def test_docs_require_password_when_configured():
    client = _client("s3cret")
    assert client.get("/docs").status_code == 401
    assert client.get("/openapi.json").status_code == 401


def test_docs_accept_correct_password():
    client = _client("s3cret")
    assert client.get("/docs", headers=_basic("s3cret")).status_code == 200
    assert client.get("/openapi.json", headers=_basic("s3cret")).status_code == 200


def test_docs_reject_wrong_password():
    client = _client("s3cret")
    assert client.get("/docs", headers=_basic("nope")).status_code == 401


def test_docs_unauthorized_sets_basic_challenge():
    client = _client("s3cret")
    response = client.get("/docs")
    assert response.headers["www-authenticate"] == 'Basic realm="docs"'


def test_docs_open_without_password():
    client = _client("")
    assert client.get("/docs").status_code == 200


def test_non_docs_paths_are_not_gated():
    client = _client("s3cret")
    assert client.get("/health/live").status_code == 404


def test_docs_are_exempt_from_hmac(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_production_requires_docs_password():
    with pytest.raises(ValidationError):
        Settings(
            app_env="production",
            hmac_keys_json='{"k":"s"}',
            docs_password="",
            disable_docs=False,
        )


def test_production_allows_docs_with_password():
    settings = Settings(
        app_env="production",
        hmac_keys_json='{"k":"s"}',
        docs_password="s3cret",
        disable_docs=False,
    )
    assert settings.docs_disabled is False


def test_production_can_disable_docs_without_password():
    settings = Settings(
        app_env="production",
        hmac_keys_json='{"k":"s"}',
        docs_password="",
        disable_docs=True,
    )
    assert settings.docs_disabled is True

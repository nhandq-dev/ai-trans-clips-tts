from __future__ import annotations

import time


def test_unsigned_request_rejected(client):
    response = client.get("/v1/voices")
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_signed_request_accepted(client, signer):
    headers = signer("GET", "/v1/voices")
    response = client.get("/v1/voices", headers=headers)
    assert response.status_code == 200
    assert "languages" in response.json()


def test_replayed_nonce_rejected(client, signer):
    headers = signer("GET", "/v1/voices")
    assert client.get("/v1/voices", headers=headers).status_code == 200
    replay = client.get("/v1/voices", headers=headers)
    assert replay.status_code == 401
    assert "replay" in replay.json()["reason"]


def test_bad_signature_rejected(client, signer):
    headers = signer("GET", "/v1/voices")
    headers["X-TTS-Signature"] = "deadbeef"
    assert client.get("/v1/voices", headers=headers).status_code == 401


def test_expired_timestamp_rejected(client, signer):
    headers = signer("GET", "/v1/voices", timestamp=time.time() - 120)
    assert client.get("/v1/voices", headers=headers).status_code == 401


def test_unknown_key_rejected(client, signer):
    headers = signer("GET", "/v1/voices", key_id="unknown-key")
    assert client.get("/v1/voices", headers=headers).status_code == 401


def test_health_endpoints_are_unauthenticated(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200

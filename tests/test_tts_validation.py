from __future__ import annotations

import json


def _post(client, signer, payload=None, *, raw=None, content_type="application/json"):
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = signer("POST", "/v1/tts", body)
    headers["Content-Type"] = content_type
    return client.post("/v1/tts", content=body, headers=headers)


def test_unknown_language_rejected(client, signer):
    response = _post(client, signer, {"text": "hi", "language": "xx"})
    assert response.status_code == 400
    assert "unsupported language" in response.json()["error"]


def test_unsupported_format_rejected(client, signer):
    response = _post(client, signer, {"text": "hi", "format": "ogg"})
    assert response.status_code == 400
    assert "format" in response.json()["error"]


def test_invalid_voice_rejected(client, signer):
    response = _post(client, signer, {"text": "hi", "language": "vi", "voice": "NotAVoice"})
    assert response.status_code == 400
    assert "voice" in response.json()["error"]


def test_text_over_sync_limit_rejected(client, signer):
    response = _post(client, signer, {"text": "a" * 3000})
    assert response.status_code == 400
    assert "synchronous" in response.json()["error"]


def test_text_over_async_limit_rejected(client, signer):
    """The queued path has its own ceiling (`ASYNC_MAX_TEXT_LENGTH`)."""
    body = json.dumps({"text": "a" * 30001}).encode()
    headers = signer("POST", "/v1/tts/jobs", body)
    headers["Content-Type"] = "application/json"
    response = client.post("/v1/tts/jobs", content=body, headers=headers)
    assert response.status_code == 400
    assert "asynchronous" in response.json()["error"]


def test_malformed_json_rejected(client, signer):
    response = _post(client, signer, raw=b"{not valid json")
    assert response.status_code == 422


def test_oversized_body_rejected(client):
    limit = get_settings_limit()
    body = b"x" * (limit + 1)
    response = client.post("/v1/tts", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def get_settings_limit() -> int:
    from app.core.config import get_settings

    return get_settings().request_max_body_bytes

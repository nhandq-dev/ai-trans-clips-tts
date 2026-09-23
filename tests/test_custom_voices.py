from __future__ import annotations


def _post_clone(client, signer, query: str, body: bytes = b"audio-bytes"):
    path = f"/v1/voices/clone?{query}"
    headers = signer("POST", path, body)
    headers["Content-Type"] = "application/octet-stream"
    return client.post(path, content=body, headers=headers)


def test_clone_requires_owner(client, signer):
    path = "/v1/voices/clone"
    body = b"x"
    headers = signer("POST", path, body)
    response = client.post(path, content=body, headers=headers)
    assert response.status_code == 400
    assert "owner" in response.json()["error"]


def test_clone_rejects_non_vietnamese(client, signer):
    response = _post_clone(client, signer, "owner=1&language=en&name=Test")
    assert response.status_code == 400
    assert "Vietnamese" in response.json()["error"]


def test_clone_requires_name(client, signer):
    response = _post_clone(client, signer, "owner=1&language=vi")
    assert response.status_code == 400
    assert "name" in response.json()["error"]


def test_clone_requires_audio(client, signer):
    response = _post_clone(client, signer, "owner=1&language=vi&name=Test", body=b"")
    assert response.status_code == 400
    assert "audio" in response.json()["error"]


def test_clone_is_hmac_protected(client):
    path = "/v1/voices/clone?owner=1&language=vi&name=Test"
    response = client.post(path, content=b"x", headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 401


def test_sample_unknown_voice_returns_404(client, signer):
    path = f"/v1/voices/clone/{'0' * 32}/sample?owner=1"
    response = client.get(path, headers=signer("GET", path))
    assert response.status_code == 404


def test_sample_rejects_invalid_id(client, signer):
    path = "/v1/voices/clone/not-a-voice/sample?owner=1"
    response = client.get(path, headers=signer("GET", path))
    assert response.status_code == 404


def test_delete_unknown_voice_returns_404(client, signer):
    path = f"/v1/voices/clone/{'a' * 32}?owner=1"
    response = client.delete(path, headers=signer("DELETE", path))
    assert response.status_code == 404


def test_tts_with_unknown_custom_voice_is_rejected(client, signer):
    import json

    payload = {"text": "Xin chào", "language": "vi", "voice": "b" * 32, "owner": 1}
    body = json.dumps(payload).encode()
    path = "/v1/tts"
    headers = signer("POST", path, body)
    headers["Content-Type"] = "application/json"
    response = client.post(path, content=body, headers=headers)
    assert response.status_code == 400
    assert "voice" in response.json()["error"]

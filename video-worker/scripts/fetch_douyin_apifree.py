"""Download a Douyin video via a free third-party resolver (tainhanhvideo.com).

Douyin 403s non-China datacenter IPs for its own web API (all tiers:
yt-dlp / f2 / douyin-downloader). This resolver server does the signed API
call from its (working) location and returns an expiring CDN video URL, which
we then download directly — no proxy, no logged-in cookies required.

The resolver added an anti-bot handshake in 2026-10: a page nonce signed by the
server, bound to the session cookie, must be at least ``min_age`` seconds old
and is sent as ``X-Page-Nonce`` together with ``X-CSRF-TOKEN``. Posting without
it returns ``403 {"code": "HUMAN_CHECK_FAILED"}``.

Flow:
  1. GET  /                      -> session cookie + CSRF _token + client version
  2. GET  /tiktok/nonce          -> {nonce, min_age, ttl}
  3. wait min_age seconds
  4. POST /tiktok/download       -> {url, type: douyin, website: "", _token}
                                    headers: X-CSRF-TOKEN, X-Page-Nonce,
                                    X-Client-Version
  5. download the returned `video_url` (absolute, or `/tiktok/stream/<token>`
     relative to BASE) to output_path

Exit code 0 on success, non-zero on failure. Best-effort: the worker falls
back to the other Douyin tiers if this resolver is down.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import httpx

BASE = (os.getenv("DOUYIN_APIFREE_BASE") or "https://tainhanhvideo.com").rstrip("/")
DOWNLOAD_ENDPOINT = f"{BASE}/tiktok/download"
NONCE_ENDPOINT = f"{BASE}/tiktok/nonce"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
API_TIMEOUT = float(os.getenv("DOUYIN_APIFREE_API_TIMEOUT_SECONDS", "30"))
DOWNLOAD_TIMEOUT = float(os.getenv("DOUYIN_APIFREE_DOWNLOAD_TIMEOUT_SECONDS", "300"))
MAX_BYTES = int(os.getenv("MAX_FILESIZE_MB", "500")) * 1024 * 1024
DEFAULT_CLIENT_VERSION = "1792741633"
MAX_NONCE_WAIT_SECONDS = 15.0
MEDIA_FIELDS = ("video_url", "video_hd", "video_download_url", "download_url")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _csrf_token(html: str) -> str | None:
    m = re.search(r'name=["\']csrf-token["\'][^>]*content=["\']([A-Za-z0-9]+)["\']', html)
    if m:
        return m.group(1)
    m = re.search(r'<meta[^>]*content=["\']([A-Za-z0-9]{40})["\'][^>]*csrf', html)
    return m.group(1) if m else None


def _client_version(html: str) -> str:
    """The site rejects stale client versions; read it from the page when possible."""
    m = re.search(r"X-Client-Version['\"]?\s*:\s*['\"](\d+)['\"]", html)
    return m.group(1) if m else DEFAULT_CLIENT_VERSION


def _ajax_headers(token: str, nonce: str, client_version: str) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": f"{BASE}/",
        "User-Agent": UA,
        "X-Client-Version": client_version,
        "X-CSRF-TOKEN": token,
        "X-Requested-With": "XMLHttpRequest",
    }
    if nonce:
        headers["X-Page-Nonce"] = nonce
    return headers


def _fresh_nonce(client: httpx.Client, token: str, client_version: str) -> str:
    resp = client.get(
        NONCE_ENDPOINT,
        headers=_ajax_headers(token, "", client_version),
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    nonce = payload.get("nonce")
    if not nonce:
        raise ValueError("nonce missing from /tiktok/nonce")
    try:
        wait = min(float(payload.get("min_age") or 0) + 0.2, MAX_NONCE_WAIT_SECONDS)
    except (TypeError, ValueError):
        wait = 2.2
    if wait > 0:
        time.sleep(wait)
    return str(nonce)


def _absolute(media_url: str) -> str:
    if media_url.startswith("http://") or media_url.startswith("https://"):
        return media_url
    if media_url.startswith("/"):
        return BASE + media_url
    return f"{BASE}/{media_url}"


def _resolve(client: httpx.Client, url: str, token: str, client_version: str) -> dict:
    """POST the resolver, retrying once with a new nonce when the human check fails."""
    last_error = ""
    for attempt in (1, 2):
        nonce = _fresh_nonce(client, token, client_version)
        resp = client.post(
            DOWNLOAD_ENDPOINT,
            data={"url": url, "type": "douyin", "website": "", "_token": token},
            headers=_ajax_headers(token, nonce, client_version),
        )
        if resp.status_code == 419:
            raise RuntimeError("csrf token rotated mid-flow")
        if resp.status_code == 403 and "HUMAN_CHECK_FAILED" in resp.text:
            last_error = resp.text[:200]
            log(f"human check failed (attempt {attempt}); refreshing nonce")
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"resolver HTTP {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        payload = resp.json() or {}
        if not payload.get("status"):
            raise RuntimeError(f"resolver error: {str(payload.get('message'))[:200]}")
        return payload.get("data") or {}
    raise RuntimeError(f"human check still failing: {last_error}")


def _download(client: httpx.Client, media_url: str, output_path: Path) -> bool:
    with client.stream(
        "GET",
        media_url,
        headers={"Referer": f"{BASE}/", "User-Agent": UA},
        timeout=DOWNLOAD_TIMEOUT,
    ) as resp:
        resp.raise_for_status()
        length = resp.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BYTES:
            log(f"file too large: {length} bytes")
            return False
        total = 0
        with open(output_path, "wb") as fh:
            for chunk in resp.iter_bytes(1024 * 256):
                total += len(chunk)
                if total > MAX_BYTES:
                    log(f"file too large: exceeded {MAX_BYTES} bytes")
                    return False
                fh.write(chunk)
    return True


def main() -> int:
    if len(sys.argv) < 3:
        log("usage: fetch_douyin_apifree.py <url> <output_path>")
        return 1
    url = sys.argv[1]
    output_path = Path(sys.argv[2])

    try:
        with httpx.Client(
            headers={"User-Agent": UA}, timeout=API_TIMEOUT, follow_redirects=True
        ) as client:
            # 1) session cookie + CSRF token + client version
            page = client.get(f"{BASE}/")
            page.raise_for_status()
            token = _csrf_token(page.text)
            if not token:
                log("no csrf token in page")
                return 1
            client_version = _client_version(page.text)

            # 2-4) nonce-bound resolve (retry the whole flow once on a rotated token)
            try:
                data = _resolve(client, url, token, client_version)
            except RuntimeError:
                page = client.get(f"{BASE}/")
                page.raise_for_status()
                token = _csrf_token(page.text)
                if not token:
                    raise
                data = _resolve(client, url, token, client_version)

            media_url = next(
                (data.get(field) for field in MEDIA_FIELDS if data.get(field)),
                None,
            )
            if not media_url:
                if data.get("images"):
                    log("resolver returned an image gallery, not a video")
                else:
                    log(f"resolver returned no video_url: {str(data)[:200]}")
                return 1

            media_url = _absolute(str(media_url))
            log(f"resolved: {media_url[:120]}")

            # 5) download the CDN / token-stream file
            if not _download(client, media_url, output_path):
                output_path.unlink(missing_ok=True)
                return 1
    except Exception as exc:
        log(f"apifree error: {type(exc).__name__}: {exc}")
        output_path.unlink(missing_ok=True)
        return 1

    ok = output_path.exists() and output_path.stat().st_size > 1024
    log(f"apifree final: exists={output_path.exists()} ok={ok}")
    if not ok:
        output_path.unlink(missing_ok=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

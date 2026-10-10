"""Download a Douyin video via a free third-party resolver (tainhanhvideo.com).

Douyin 403s non-China datacenter IPs for its own web API (all tiers:
yt-dlp / f2 / douyin-downloader). This resolver server does the signed API
call from its (working) location and returns an expiring CDN video URL, which
we then download directly — no proxy, no logged-in cookies required.

Flow:
  1. GET https://tainhanhvideo.com/  -> grab session cookie + CSRF _token
  2. POST https://tainhanhvideo.com/tiktok/download -> {url, type: douyin, _token}
  3. download the returned `video_url` (mp4) to output_path

Exit code 0 on success, non-zero on failure. Best-effort: the worker falls
back to the other Douyin tiers if this resolver is down.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import httpx

BASE = "https://tainhanhvideo.com"
DOWNLOAD_ENDPOINT = f"{BASE}/tiktok/download"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
TIMEOUT = 30


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _csrf_token(html: str) -> str | None:
    m = re.search(r'name=["\']csrf-token["\'][^>]*content=["\']([A-Za-z0-9]+)["\']', html)
    if m:
        return m.group(1)
    m = re.search(r'<meta[^>]*content=["\']([A-Za-z0-9]{40})["\'][^>]*csrf', html)
    return m.group(1) if m else None


def main() -> int:
    if len(sys.argv) < 3:
        log("usage: fetch_douyin_apifree.py <url> <output_path>")
        return 1
    url = sys.argv[1]
    output_path = Path(sys.argv[2])

    try:
        with httpx.Client(
            headers={"User-Agent": UA}, timeout=TIMEOUT, follow_redirects=True
        ) as client:
            # 1) session + CSRF token
            page = client.get(BASE)
            page.raise_for_status()
            token = _csrf_token(page.text)
            if not token:
                log("no csrf token in page")
                return 1

            # 2) resolve the video
            resp = client.post(
                DOWNLOAD_ENDPOINT,
                data={"url": url, "type": "douyin", "_token": token},
                headers={"Referer": BASE, "X-Requested-With": "XMLHttpRequest"},
            )
            if resp.status_code == 419:
                log("csrf mismatch (token rotated mid-flow)")
                return 1
            resp.raise_for_status()
            data = (resp.json() or {}).get("data") or {}
            video_url = (
                data.get("video_url") or data.get("video_hd") or data.get("video_download_url")
            )
            if not video_url or not resp.json().get("status"):
                log(f"resolver returned no video_url: {resp.text[:200]}")
                return 1

            # 3) download the CDN file
            with client.stream("GET", video_url) as dl:
                dl.raise_for_status()
                with open(output_path, "wb") as fh:
                    for chunk in dl.iter_bytes(1024 * 256):
                        fh.write(chunk)
    except Exception as exc:
        log(f"apifree error: {type(exc).__name__}: {exc}")
        output_path.unlink(missing_ok=True)
        return 1

    ok = output_path.exists() and output_path.stat().st_size > 1024
    log(f"apifree final: exists={output_path.exists()} ok={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

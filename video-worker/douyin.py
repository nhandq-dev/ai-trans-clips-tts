"""Douyin-specific extraction.

Download chain (see `downloader._download_douyin`):

    1. douyin-downloader (jiji262) — logged-in jar, then anonymous
    2. f2 (a_bogus signing)        — logged-in jar, then anonymous
    3. yt-dlp                      — with cookies, refreshed once

No browser is ever launched. This module also provides the best-effort metadata
used by `/info` (the iesdouyin SSR page, falling back to tikwm).
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import httpx

UA_DESKTOP = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)

AWEME_RE = re.compile(r"(?:douyin\.com/video/|iesdouyin\.com/share/video/)(\d{15,})")
_ANY_ID_RE = re.compile(r"(\d{15,})")

SCRIPTS_DIR = Path(__file__).parent / "scripts"


# --------------------------------------------------------------------------
# URL / id helpers
# --------------------------------------------------------------------------


def resolve_short_url(url: str) -> str:
    if "v.douyin.com" not in url:
        return url
    try:
        resp = httpx.get(url, follow_redirects=False, timeout=10)
        return resp.headers.get("location") or url
    except Exception:
        return url


def resolve_aweme_id(url: str) -> str | None:
    m = AWEME_RE.search(resolve_short_url(url))
    if m:
        return m.group(1)
    m = _ANY_ID_RE.search(url)
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# iesdouyin SSR (used by /info for a title/cover preview)
# --------------------------------------------------------------------------


def extract_router_data(text: str) -> dict | None:
    idx = text.find("_ROUTER_DATA")
    if idx < 0:
        return None
    start = text.find("{", idx)
    if start < 0:
        return None
    depth = 0
    end = start
    for i, c in enumerate(text[start : start + 50000]):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = start + i + 1
                break
    try:
        return json.loads(text[start:end].replace("\\u002F", "/"))
    except json.JSONDecodeError:
        return None


def fetch_metadata(aweme_id: str) -> dict | None:
    """Best-effort metadata from the iesdouyin share page (no signing needed).

    Used by /info, where a yt-dlp probe fails because Douyin needs a signed
    (a_bogus) request. Returns None when the page cannot be parsed.
    """
    url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
    headers = {"User-Agent": UA_MOBILE, "Referer": "https://www.douyin.com/"}
    try:
        resp = httpx.get(url, headers=headers, follow_redirects=True, timeout=15)
        resp.raise_for_status()
    except Exception:
        return None

    data = extract_router_data(resp.text)
    if not data:
        return None

    for val in (data.get("loaderData") or {}).values():
        if not isinstance(val, dict):
            continue
        items = (val.get("videoInfoRes") or {}).get("item_list") or []
        if not items:
            continue
        item = items[0] or {}
        video = item.get("video") or {}
        covers = (video.get("cover") or {}).get("url_list") or []
        duration_ms = video.get("duration") or 0
        return {
            "title": item.get("desc") or None,
            "duration": (duration_ms / 1000) if duration_ms else None,
            "thumbnail": covers[0] if covers else None,
            "uploader": ((item.get("author") or {}).get("nickname")) or None,
            "ext": "mp4",
            "platform": "douyin",
        }
    return None


def fetch_metadata_public(url: str) -> dict | None:
    """Metadata via tikwm (no cookies, no signing).

    The iesdouyin SSR payload no longer carries `item_list`, so this is the
    reliable way to preview a Douyin link in /info.
    """
    try:
        resp = httpx.get(
            f"https://www.tikwm.com/api/?url={url}",
            headers={"User-Agent": UA_DESKTOP},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = (resp.json() or {}).get("data") or {}
        if not data:
            return None
        author = data.get("author")
        uploader = author.get("nickname") if isinstance(author, dict) else (author or None)
        return {
            "title": data.get("title") or None,
            "duration": data.get("duration") or None,
            "thumbnail": data.get("cover") or None,
            "uploader": uploader,
            "ext": "mp4",
            "platform": "douyin",
        }
    except Exception:
        return None


# --------------------------------------------------------------------------
# Downloaders
# --------------------------------------------------------------------------


async def _run_script(script: Path, args: list[str], output_path: Path, timeout: int) -> bool:
    """Run one of the Douyin helper scripts; success = a non-trivial output file."""
    if not script.is_file():
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            output_path.unlink(missing_ok=True)
            return False
        if proc.returncode == 0 and output_path.exists() and output_path.stat().st_size > 1024:
            return True
    except Exception:
        pass
    output_path.unlink(missing_ok=True)
    return False


async def try_douyin_downloader(
    url: str, output_path: Path, cookie_file: str, timeout: int = 180
) -> bool:
    """jiji262 `douyin-downloader` — first tier for Douyin."""
    return await _run_script(
        SCRIPTS_DIR / "fetch_douyin_downloader.py",
        [url, str(output_path), cookie_file],
        output_path,
        timeout,
    )


async def try_f2(url: str, output_path: Path, cookie_file: str, timeout: int = 600) -> bool:
    """f2 library (a_bogus signing). Requires a logged-in jar — Douyin 403s otherwise."""
    if not cookie_file:
        return False
    return await _run_script(
        SCRIPTS_DIR / "fetch_douyin_f2.py",
        [url, str(output_path), cookie_file],
        output_path,
        timeout,
    )

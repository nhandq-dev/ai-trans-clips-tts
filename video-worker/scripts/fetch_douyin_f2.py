"""Download a Douyin video via the f2 library (a_bogus signing).

yt-dlp's Douyin extractor cannot generate the a_bogus signature, and Douyin's
web API returns 403 / empty 200 responses to non-China datacenter IPs without a
logged-in session. f2 signs requests with a_bogus; combined with a real
logged-in cookie jar (sessionid/sid_guard/uid_tt) the API returns full video
metadata and we download the clean play URL directly.

Usage: python fetch_douyin_f2.py <url> <output_path> [cookie_file]
Exit code 0 on success, non-zero on failure.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

COOKIE_DIR = Path(os.getenv("COOKIE_DIR") or (Path(__file__).parent.parent / "cookies"))
DEFAULT_COOKIE_FILE = str(COOKIE_DIR / "douyin_logged_in.txt")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _netscape_to_str(path: str) -> str:
    parts = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                cols = line.split("\t")
                if len(cols) >= 7:
                    parts.append(f"{cols[5]}={cols[6]}")
    except OSError:
        pass
    return "; ".join(parts)


def _resolve_aweme_id(url: str) -> str | None:
    m = re.search(r"(\d{15,})", url)
    if m:
        return m.group(1)
    try:
        import httpx

        resp = httpx.get(url, follow_redirects=False, timeout=10)
        m = re.search(r"(\d{15,})", resp.headers.get("location", ""))
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _clean_play_url(url: str) -> str:
    return str(url).replace("playwm", "play")


def _pick_no_watermark_urls(video: dict, f2_play_addrs: list | None = None) -> list[str]:
    """Clean (no-logo) play URLs first; `download_addr` is the watermarked last resort."""
    seen: set[str] = set()
    ordered: list[str] = []

    def add(addr: dict | None) -> None:
        for u in (addr or {}).get("url_list") or []:
            u = _clean_play_url(u)
            if u and u not in seen and "playwm" not in u:
                seen.add(u)
                ordered.append(u)

    add(video.get("play_addr") or {})
    for item in video.get("bit_rate") or []:
        add((item or {}).get("play_addr") or {})
    for u in f2_play_addrs or []:
        u = _clean_play_url(u)
        if u and u not in seen and "playwm" not in u:
            seen.add(u)
            ordered.append(u)
    add(video.get("download_addr") or {})  # last resort — usually watermarked
    return ordered


async def _fetch_and_download(aweme_id: str, output_path: Path, cookie_file: str) -> bool:
    import httpx
    from f2.apps.douyin.crawler import DouyinCrawler
    from f2.apps.douyin.filter import PostDetailFilter
    from f2.apps.douyin.model import PostDetail

    cookie = _netscape_to_str(cookie_file)
    if not cookie:
        log(f"no cookie file found: {cookie_file}")
        return False
    log(f"using cookies: {cookie_file}")

    kwargs = {
        "cookie": cookie,
        "headers": {"User-Agent": _UA},
        "proxies": {"http://": None, "https://": None},
        "timeout": 10,
    }

    async with DouyinCrawler(kwargs) as crawler:
        resp = await crawler.fetch_post_detail(PostDetail(aweme_id=aweme_id))
        v = PostDetailFilter(resp)
        video = (v._to_raw().get("aweme_detail", {}) or {}).get("video", {}) or {}
        play_urls = _pick_no_watermark_urls(video, f2_play_addrs=list(v.video_play_addr or []))

    if not play_urls:
        log("no play url in metadata")
        return False

    headers = {"User-Agent": _UA, "Referer": "https://www.douyin.com/"}
    for play_url in play_urls:
        log(f"try play url: {play_url[:120]}")
        try:
            with httpx.stream(
                "GET", play_url, headers=headers, follow_redirects=True, timeout=180
            ) as r:
                r.raise_for_status()
                with open(output_path, "wb") as fh:
                    for chunk in r.iter_bytes(1024 * 256):
                        fh.write(chunk)
            if output_path.exists() and output_path.stat().st_size > 1024:
                return True
        except Exception as exc:
            log(f"download error: {type(exc).__name__}: {exc} ({play_url[:80]})")
            output_path.unlink(missing_ok=True)
            continue
    return False


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: fetch_douyin_f2.py <url> <output_path> [cookie_file]", file=sys.stderr)
        return 1
    url = sys.argv[1]
    output_path = Path(sys.argv[2])
    cookie_file = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_COOKIE_FILE
    aweme_id = _resolve_aweme_id(url)
    if not aweme_id:
        log("cannot resolve aweme_id")
        return 1
    log(f"aweme_id: {aweme_id}")
    return 0 if asyncio.run(_fetch_and_download(aweme_id, output_path, cookie_file)) else 1


if __name__ == "__main__":
    sys.exit(main())

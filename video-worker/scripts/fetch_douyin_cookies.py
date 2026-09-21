"""Fetch anonymous Douyin cookies into the worker cookie store.

Douyin's web extractor needs a browser session ("Fresh cookies are needed").
This uses Playwright + headless Chromium to visit douyin.com and dumps the
resulting cookies as a Netscape jar so yt-dlp / douyin-downloader can read them.

Ported from video-translator-saas/ai-worker/scripts/fetch_douyin_cookies.py;
now writes into COOKIE_DIR instead of hardcoded /opt paths.

Exit code 0 on success, non-zero on failure.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

COOKIE_DIR = Path(os.getenv("COOKIE_DIR") or (Path(__file__).parent.parent / "cookies"))
# Anonymous jar only. `douyin_logged_in.txt` must come from a real browser
# session (export-brave-cookies.sh) — f2 gets a 403 with anonymous cookies, so
# writing them there would only mislead the fallback chain.
OUTPUT = COOKIE_DIR / "douyin.txt"

DOUYIN_URLS = ["https://www.douyin.com/", "https://www.douyin.com/discover"]


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed", file=sys.stderr)
        return 1

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
            page = context.new_page()
            for url in DOUYIN_URLS:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                except Exception:
                    continue
            page.wait_for_timeout(3000)
            cookies = context.cookies()
            browser.close()
    except Exception as exc:
        print(f"playwright error: {exc}", file=sys.stderr)
        return 1

    if not cookies:
        print("no cookies collected", file=sys.stderr)
        return 1

    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        domain = c.get("domain", "")
        if not domain:
            continue
        lines.append(
            "\t".join(
                [
                    domain,
                    "TRUE" if domain.startswith(".") else "FALSE",
                    c.get("path", "/"),
                    "TRUE" if c.get("secure") else "FALSE",
                    str(max(int(c.get("expires", 0)), 0)),
                    c.get("name", ""),
                    c.get("value", ""),
                ]
            )
        )

    payload = "\n".join(lines) + "\n"
    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(payload, encoding="utf-8")
    print(f"wrote {len(cookies)} anonymous cookies to {OUTPUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Cookie store resolution.

Cookies live as Netscape jars on disk, one per platform plus a `combined.txt`
fallback, and are read at request time. Updating a file takes effect on the
next download — no restart, no rebuild. Managed by `scripts/cookiectl` (VPS)
and `scripts/export-brave-cookies.sh` (developer machine).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

COOKIE_DIR = Path(os.getenv("COOKIE_DIR") or (Path(__file__).parent / "cookies"))
SCRIPTS_DIR = Path(__file__).parent / "scripts"

# f2 needs a *logged-in* jar (sessionid/sid_guard) for best results;
# anonymous cookies only work for yt-dlp's first attempt.
DOUYIN_LOGGED_IN = "douyin_logged_in.txt"

# Legacy paths from the old Update-Douyin-Cookie.command flow.
# The new worker pushes to COOKIE_DIR, but we keep these as fallbacks so your
# existing `Update-Douyin-Cookie.command` keeps working during migration.
LEGACY_COOKIES = Path("/opt/douyin_cookies.txt")
LEGACY_LOGGED_IN = Path("/opt/douyin_cookies_logged_in.txt")


def _readable(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 100
    except OSError:
        return False


def cookie_file_for(platform: str | None) -> str:
    """Return the jar path for a platform, falling back to combined.txt, then legacy."""
    if platform:
        candidate = COOKIE_DIR / f"{platform}.txt"
        if _readable(candidate):
            return str(candidate)
        # Legacy fallback: old command pushes a single douyin.txt with all douyin cookies
        if platform == "douyin" and _readable(LEGACY_COOKIES):
            return str(LEGACY_COOKIES)
    combined = COOKIE_DIR / "combined.txt"
    if _readable(combined):
        return str(combined)
    # Legacy also pushed the same file to combined-equivalent? Use it as last resort
    if _readable(LEGACY_COOKIES):
        return str(LEGACY_COOKIES)
    return ""


def douyin_logged_in_file() -> str:
    path = COOKIE_DIR / DOUYIN_LOGGED_IN
    if _readable(path):
        return str(path)
    if _readable(LEGACY_LOGGED_IN):
        return str(LEGACY_LOGGED_IN)
    return ""


async def refresh_douyin_cookies(timeout: int = 90) -> str:
    """Run the Playwright cookie fetcher and return the refreshed jar path.

    Best-effort: returns '' when the script is missing or fails, so callers can
    continue down the fallback chain instead of aborting the request.
    """
    script = SCRIPTS_DIR / "fetch_douyin_cookies.py"
    if not script.is_file():
        return ""
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            return ""
    except Exception:
        return ""
    return cookie_file_for("douyin")

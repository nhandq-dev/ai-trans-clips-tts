"""Download a Douyin video via the jiji262 `douyin-downloader` package.

This is the modern, maintained project (https://github.com/jiji262/douyin-downloader),
NOT the 2018 PyPI package that shares the name — installing the latter by name
gets the wrong library. Install with:
    pip install 'douyin-downloader @ git+https://github.com/jiji262/douyin-downloader'

It reads cookies from a Netscape jar and writes a temporary config.yml.
Its browser fallback (Playwright/Chromium) is deliberately DISABLED — the worker
must not launch a browser.

Usage: python fetch_douyin_downloader.py <url> <output_path> [cookie_file]
Exit code 0 on success, non-zero on failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

COOKIE_DIR = Path(os.getenv("COOKIE_DIR") or (Path(__file__).parent.parent / "cookies"))
DEFAULT_COOKIE_FILE = str(COOKIE_DIR / "douyin_logged_in.txt")

# Only the cookies the tool's config knows about; msToken is minted per request.
COOKIE_KEYS = ("msToken", "ttwid", "odin_tt", "passport_csrf_token", "sid_guard")

TIMEOUT_SECONDS = int(os.getenv("DOUYIN_DOWNLOADER_TIMEOUT_SECONDS", "180"))


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _read_netscape(path: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                cols = line.split("\t")
                if len(cols) >= 7:
                    cookies[cols[5]] = cols[6]
    except OSError:
        pass
    return cookies


def _write_config(config_path: Path, out_dir: Path, cookies: dict[str, str]) -> None:
    cookie_lines = "\n".join(f"  {key}: {json_quote(cookies.get(key, ''))}" for key in COOKIE_KEYS)
    config_path.write_text(
        f"""path: {json_quote(str(out_dir) + "/")}
video: true
music: false
cover: false
avatar: false
json: false
folderstyle: false
proxy: ""
database: false
video_quality: highest
progress:
  quiet_logs: true
# Never launch a browser in the worker.
browser_fallback:
  enabled: false
cookies:
{cookie_lines}
""",
        encoding="utf-8",
    )


def json_quote(value: str) -> str:
    """Minimal YAML-safe double-quoted scalar."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _newest_mp4(root: Path) -> Path | None:
    files = [p for p in root.rglob("*.mp4") if p.is_file() and p.stat().st_size > 1024]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def _entrypoint() -> list[str]:
    """Prefer the installed console script, fall back to the module."""
    exe = shutil.which("douyin-dl")
    if exe:
        return [exe]
    return [sys.executable, "-m", "cli.main"]


def main() -> int:
    if len(sys.argv) < 3:
        print(
            "usage: fetch_douyin_downloader.py <url> <output_path> [cookie_file]", file=sys.stderr
        )
        return 1

    url = sys.argv[1]
    output_path = Path(sys.argv[2])
    cookie_file = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_COOKIE_FILE

    cookies = _read_netscape(cookie_file)
    if not cookies:
        log(f"no cookie file found: {cookie_file}")
        return 1

    with tempfile.TemporaryDirectory(prefix="dydl-") as tmp:
        tmp_dir = Path(tmp)
        config_path = tmp_dir / "config.yml"
        download_dir = tmp_dir / "out"
        download_dir.mkdir()
        _write_config(config_path, download_dir, cookies)

        cmd = [
            *_entrypoint(),
            "-c",
            str(config_path),
            "-u",
            url,
            "-p",
            str(download_dir),
        ]
        log(f"running: {' '.join(cmd[:3])} ...")
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
                cwd=str(tmp_dir),
            )
        except subprocess.TimeoutExpired:
            log(f"timed out after {TIMEOUT_SECONDS}s")
            return 1
        except FileNotFoundError as exc:
            log(f"douyin-downloader not installed: {exc}")
            return 1

        if proc.returncode != 0:
            log(f"exit={proc.returncode} stderr={proc.stderr[-400:]}")

        found = _newest_mp4(download_dir)
        if found is None:
            log("no mp4 produced")
            return 1

        shutil.move(str(found), str(output_path))

    ok = output_path.exists() and output_path.stat().st_size > 1024
    log(f"final: exists={output_path.exists()} ok={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

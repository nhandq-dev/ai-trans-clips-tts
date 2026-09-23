"""Download orchestration.

Adapted from video-translator-saas/ai-worker/routes/download.py:
- dropped R2 persist, BullMQ/Redis, GPU/backend coupling and job-choreography
- cookies now resolve from the on-disk cookie store (see cookies.py)
- added an explicit platform allowlist (see platforms.py)
- general platforms: the yt-dlp trial matrix (plain → cookies → proxy → cookies+proxy)
- Douyin: douyin-downloader → f2 → yt-dlp, then a typed error. No browser.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import douyin
import httpx
from cookies import cookie_file_for, douyin_logged_in_file, refresh_douyin_cookies
from errors import DownloadError, classify
from platforms import platform_of, validate_url

logger = logging.getLogger("video-worker")


@dataclass
class DownloadResult:
    """A finished download.

    `from_cache` marks files owned by the dedupe cache: they must NOT be
    deleted after streaming, and are reused across requests.
    """

    path: Path
    filename: str
    from_cache: bool = False


OUTPUT_DIR = Path(os.getenv("VIDEO_OUTPUT_DIR") or (Path(__file__).parent / "output"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

CACHE_TTL_SECONDS = int(os.getenv("VIDEO_CACHE_TTL_SECONDS", "86400"))
CACHE_DIR = OUTPUT_DIR / ".cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MAX_BYTES = int(os.getenv("MAX_FILESIZE_MB", "500")) * 1024 * 1024
MAX_FILESIZE_ARG = f"{os.getenv('MAX_FILESIZE_MB', '500')}M"
YTDLP_TIMEOUT = int(os.getenv("YTDLP_TIMEOUT_SECONDS", "600"))

YTDLP_BASE = [sys.executable, "-m", "yt_dlp"]

# Format preference, most compatible first.
#
# YouTube serves `bestvideo` as VP9/AV1 and `bestaudio` as Opus. Merging those
# into an MP4 produces a file QuickTime/Safari/iOS/Windows refuse to play (VP9
# and Opus are not valid MP4 codecs for those players). Prefer H.264 (avc1) +
# AAC (mp4a) so the downloaded file plays everywhere; fall back to other codecs
# only when no H.264 rendition exists.
FORMATS = (
    "bestvideo[vcodec^=avc1][height<=1080]+bestaudio[acodec^=mp4a]/best[ext=mp4][height<=1080]",
    "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/best[ext=mp4]",
    "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
    "bestvideo+bestaudio/best",
)

DIRECT_EXTENSIONS = {
    ".mp4",
    ".webm",
    ".mkv",
    ".mov",
    ".avi",
    ".wmv",
    ".flv",
    ".m4v",
    ".mp3",
    ".wav",
    ".ogg",
    ".aac",
    ".m4a",
}


def output_dir() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR


def _existing(job_id: str) -> list[Path]:
    return sorted(output_dir().glob(f"{job_id}_*"))


def _check_size(path: Path) -> Path:
    if path.exists() and path.stat().st_size > MAX_BYTES:
        path.unlink(missing_ok=True)
        raise DownloadError("TOO_LARGE")
    return path


def _is_direct_media(url: str) -> bool:
    path = url.split("?")[0].lower()
    return any(path.endswith(ext) for ext in DIRECT_EXTENSIONS)


def _ytdlp_args(url: str, fmt: str, template: str, cookie_file: str, proxy: str) -> list[str]:
    args = [
        *YTDLP_BASE,
        "-f",
        fmt,
        "--merge-output-format",
        "mp4",
        "--output",
        template,
        "--no-playlist",
        "--no-warnings",
        "--max-filesize",
        MAX_FILESIZE_ARG,
    ]
    if cookie_file and Path(cookie_file).is_file():
        args += ["--cookies", cookie_file]
    if proxy:
        args += ["--proxy", proxy]
    args.append(url)
    return args


async def _run_ytdlp(args: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=YTDLP_TIMEOUT)
    except asyncio.TimeoutError:  # noqa: UP041 - this image runs Python 3.10
        proc.kill()
        return 1, "timed out"
    text = (
        (stderr or b"").decode(errors="replace") + "\n" + (stdout or b"").decode(errors="replace")
    ).strip()
    return int(proc.returncode or 1), text


def _adopt(job_id: str) -> Path | None:
    """Return a completed, non-empty output file for the job, else None."""
    found = _existing(job_id)
    if not found:
        return None
    first = found[0]
    _check_size(first)
    if first.stat().st_size > 1024:
        return first
    first.unlink(missing_ok=True)
    return None


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _cache_name_file(h: str) -> Path:
    """Sidecar holding the friendly filename for a cached file."""
    return CACHE_DIR / f"{h}.name"


def _find_cached(url: str) -> DownloadResult | None:
    """Return the cached file for this URL, or None. Never copies."""
    h = _url_hash(url)
    for p in CACHE_DIR.glob(f"{h}.*"):
        if p.suffix == ".name" or not p.is_file() or p.stat().st_size <= 1024:
            continue
        age = time.time() - p.stat().st_mtime
        if age >= CACHE_TTL_SECONDS:
            p.unlink(missing_ok=True)
            _cache_name_file(h).unlink(missing_ok=True)
            continue
        name_file = _cache_name_file(h)
        filename = name_file.read_text(encoding="utf-8").strip() if name_file.is_file() else p.name
        return DownloadResult(path=p, filename=filename or p.name, from_cache=True)
    return None


def _save_to_cache(url: str, src: Path, filename: str) -> None:
    try:
        h = _url_hash(url)
        dst = CACHE_DIR / f"{h}{src.suffix or '.mp4'}"
        shutil.copy2(src, dst)
        _cache_name_file(h).write_text(filename, encoding="utf-8")
    except Exception:
        pass


async def _download_direct(url: str, job_id: str) -> Path | None:
    ext = ".mp4"
    name = url.split("?")[0].rsplit("/", 1)[-1]
    if "." in name:
        ext = "." + name.rsplit(".", 1)[-1]
    dest = output_dir() / f"{job_id}_direct{ext}"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                length = resp.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > MAX_BYTES:
                    raise DownloadError("TOO_LARGE")
                with open(dest, "wb") as fh:
                    async for chunk in resp.aiter_bytes(1024 * 256):
                        fh.write(chunk)
        _check_size(dest)
        return dest if dest.stat().st_size > 1024 else None
    except DownloadError:
        raise
    except Exception:
        dest.unlink(missing_ok=True)
        return None


async def _download_douyin(url: str, job_id: str, stderr_log: list[str]) -> Path | None:
    """Douyin chain: douyin-downloader → f2 → yt-dlp. No browser is ever launched."""
    template = str(output_dir() / f"{job_id}_%(title)s.%(ext)s")
    cookies = cookie_file_for("douyin")
    logged_in = douyin_logged_in_file()
    dl_out = output_dir() / f"{job_id}_douyin.mp4"

    # 1) douyin-downloader (jiji262) — logged-in jar first, then anonymous.
    for jar in (logged_in, cookies):
        if jar and await douyin.try_douyin_downloader(url, dl_out, jar):
            logger.info("douyin tier=douyin-downloader jar=%s", Path(jar).name)
            return dl_out

    # 2) f2 (a_bogus signing) — logged-in jar first, then anonymous.
    for jar in (logged_in, cookies):
        if jar and await douyin.try_f2(url, dl_out, jar):
            logger.info("douyin tier=f2 jar=%s", Path(jar).name)
            return dl_out

    # 3) yt-dlp. Bootstrap the jar when none exists, then retry once after a refresh.
    if not cookies:
        cookies = await refresh_douyin_cookies()
    if cookies and (
        path := await _ytdlp_all_formats(url, job_id, template, cookies, "ytdlp", stderr_log)
    ):
        return path
    await refresh_douyin_cookies()
    if cookies and (
        path := await _ytdlp_all_formats(
            url, job_id, template, cookies, "ytdlp-refresh", stderr_log
        )
    ):
        return path

    return None


async def _ytdlp_all_formats(
    url: str,
    job_id: str,
    template: str,
    jar: str,
    label: str,
    stderr_log: list[str],
) -> Path | None:
    for fmt in FORMATS:
        rc, err = await _run_ytdlp(_ytdlp_args(url, fmt, template, jar, ""))
        if (path := _adopt(job_id)) is not None:
            logger.info("douyin tier=%s fmt=%s", label, fmt)
            return path
        if classify(err) == "TOO_LARGE":
            raise DownloadError("TOO_LARGE")
        if rc != 0:
            stderr_log.append(f"[douyin-{label}][{fmt}] {err[-300:]}")
    return None


def _display_name(path: Path) -> str:
    """Filename without the internal `{job_id}_` prefix."""
    return path.name.split("_", 1)[-1] or path.name


async def download(url: str, job_id: str) -> DownloadResult:
    """Download `url` and return the result. Never retries without a caller."""
    validate_url(url)
    platform = platform_of(url)
    start = time.time()

    # Resume: reuse a finished file for this job id.
    if (path := _adopt(job_id)) is not None:
        logger.info(
            "download hit=resume platform=%s job=%s size=%d", platform, job_id, path.stat().st_size
        )
        return DownloadResult(path=path, filename=_display_name(path))

    # Dedupe: serve the cached file directly — no copy, no duplicate on disk.
    if (cached := _find_cached(url)) is not None:
        logger.info(
            "download hit=url_hash platform=%s job=%s size=%d",
            platform,
            job_id,
            cached.path.stat().st_size,
        )
        return cached

    stderr_log: list[str] = []

    if _is_direct_media(url):
        path = await _download_direct(url, job_id)
        if path is not None:
            name = _display_name(path)
            _save_to_cache(url, path, name)
            logger.info(
                "download success tier=direct platform=%s job=%s duration=%.1fs size=%d",
                platform,
                job_id,
                time.time() - start,
                path.stat().st_size,
            )
            return DownloadResult(path=path, filename=name)

    if platform == "douyin":
        path = await _download_douyin(url, job_id, stderr_log)
        if path is not None:
            name = _display_name(path)
            _save_to_cache(url, path, name)
            logger.info(
                "download success tier=douyin platform=%s job=%s duration=%.1fs size=%d",
                platform,
                job_id,
                time.time() - start,
                path.stat().st_size,
            )
            return DownloadResult(path=path, filename=name)
        logger.warning(
            "download failed platform=%s job=%s duration=%.1fs errors=%s",
            platform,
            job_id,
            time.time() - start,
            "; ".join(stderr_log)[-500:],
        )
        raise DownloadError(
            "GEO_BLOCKED",
            "; ".join(stderr_log)[-1000:],
            message=(
                "Douyin could not be downloaded. Douyin blocks requests from outside "
                "China, so all three download methods failed. Try again later, or "
                "upload the video manually."
            ),
        )

    # General path: yt-dlp trials (no cookies → cookies → +proxy).
    template = str(output_dir() / f"{job_id}_%(title)s.%(ext)s")
    cookies = cookie_file_for(platform)
    proxy = os.getenv("YT_DLP_PROXY", "")
    trials: list[tuple[str, str, str]] = [("", "", "plain")]
    if cookies:
        trials.append((cookies, "", "cookies"))
    if proxy:
        trials.append(("", proxy, "proxy"))
        if cookies:
            trials.append((cookies, proxy, "cookies+proxy"))

    for jar, prox, label in trials:
        for fmt in FORMATS:
            rc, err = await _run_ytdlp(_ytdlp_args(url, fmt, template, jar, prox))
            if (path := _adopt(job_id)) is not None:
                name = _display_name(path)
                _save_to_cache(url, path, name)
                logger.info(
                    "download success tier=yt-dlp platform=%s job=%s label=%s "
                    "fmt=%s duration=%.1fs size=%d",
                    platform,
                    job_id,
                    label,
                    fmt,
                    time.time() - start,
                    path.stat().st_size,
                )
                return DownloadResult(path=path, filename=name)
            code = classify(err)
            if code == "TOO_LARGE":
                raise DownloadError("TOO_LARGE")
            if rc != 0:
                stderr_log.append(f"[{label}][{fmt}] {err[-300:]}")

    logger.warning(
        "download failed platform=%s job=%s duration=%.1fs errors=%s",
        platform,
        job_id,
        time.time() - start,
        "; ".join(stderr_log)[-500:],
    )
    raise DownloadError(classify("\n".join(stderr_log)), "\n".join(stderr_log)[-2000:])

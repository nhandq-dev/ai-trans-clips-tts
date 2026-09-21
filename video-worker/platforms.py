"""Platform detection + URL allowlist.

Ported from video-translator-saas/ai-worker/core/safety.py, trimmed to what the
downloader needs (no job_id / path sandboxing — the worker generates its own
output names and cleans them up).
"""

from __future__ import annotations

import ipaddress
import os
import socket
import urllib.parse

# Domains we accept. Kept deliberately tight: the worker fetches user-supplied
# URLs, so an allowlist is the SSRF boundary. Extend via ALLOWED_PLATFORMS env
# (comma separated suffixes).
DEFAULT_PLATFORMS: dict[str, tuple[str, ...]] = {
    "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com", "googlevideo.com"),
    "instagram": ("instagram.com", "cdninstagram.com"),
    "facebook": ("facebook.com", "fb.watch", "fbcdn.net", "fbsbx.com"),
    "tiktok": ("tiktok.com", "tiktokv.com", "tiktokcdn.com"),
    "douyin": ("douyin.com", "iesdouyin.com", "douyinpic.com", "douyinvod.com"),
    "pinterest": ("pinterest.com", "pin.it", "pinimg.com"),
    "bilibili": ("bilibili.com", "b23.tv"),
}

_EXTRA = os.getenv("ALLOWED_PLATFORMS", "").strip()
if _EXTRA:
    DEFAULT_PLATFORMS["extra"] = tuple(d.strip() for d in _EXTRA.split(",") if d.strip())


def platform_of(url: str) -> str | None:
    """Return the platform key for a URL, or None when it is not allowlisted."""
    host = (urllib.parse.urlparse(url or "").hostname or "").lower().rstrip(".")
    if not host:
        return None
    for name, suffixes in DEFAULT_PLATFORMS.items():
        for suffix in suffixes:
            if host == suffix or host.endswith("." + suffix):
                return name
    return None


def _ip_is_private(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # fail-closed
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def is_private_host(host: str) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return True
    if host == "localhost" or host.endswith((".local", ".internal", ".lan")):
        return True
    try:
        ipaddress.ip_address(host)
        return _ip_is_private(host)
    except ValueError:
        pass
    try:
        for info in socket.getaddrinfo(host, None):
            if _ip_is_private(str(info[4][0])):
                return True
    except OSError:
        return True  # fail-closed when DNS fails
    return False


def validate_url(url: str) -> str:
    """Raise ValueError unless `url` is http(s), public, and allowlisted."""
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http/https URLs are allowed")
    if not parsed.hostname or is_private_host(parsed.hostname):
        raise ValueError("URL hostname is not allowed")
    if platform_of(url) is None:
        raise ValueError("Unsupported platform")
    return url

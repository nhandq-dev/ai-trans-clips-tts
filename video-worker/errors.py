"""Typed download errors, mapped from yt-dlp stderr for a clean UI."""

from __future__ import annotations

# code -> (http status, user-facing message)
CATALOG: dict[str, tuple[int, str]] = {
    "UNSUPPORTED": (400, "This link is not supported."),
    "INVALID_URL": (400, "The link is not valid."),
    "PRIVATE": (422, "This video is private."),
    "LOGIN_REQUIRED": (422, "This video requires a login."),
    "COOKIE_EXPIRED": (422, "The platform session expired. Please reconnect."),
    "GEO_BLOCKED": (451, "This video is not available from our region."),
    "RATE_LIMITED": (429, "The platform is rate-limiting us. Try again shortly."),
    "TOO_LARGE": (413, "The video exceeds the size limit."),
    "TIMEOUT": (504, "The download timed out."),
    "UPSTREAM": (502, "The downloader is unavailable."),
    "FAILED": (502, "The video could not be downloaded."),
}

_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("larger than max-filesize", "TOO_LARGE"),
    ("sign in to confirm", "LOGIN_REQUIRED"),
    ("login required", "LOGIN_REQUIRED"),
    ("this video is private", "PRIVATE"),
    ("private video", "PRIVATE"),
    ("fresh cookies", "COOKIE_EXPIRED"),
    ("cookies are needed", "COOKIE_EXPIRED"),
    ("not a bot", "LOGIN_REQUIRED"),
    ("account", "LOGIN_REQUIRED"),
    ("rate-limit", "RATE_LIMITED"),
    ("too many requests", "RATE_LIMITED"),
    ("http error 429", "RATE_LIMITED"),
    ("not available in your country", "GEO_BLOCKED"),
    ("geo", "GEO_BLOCKED"),
    ("unsupported url", "UNSUPPORTED"),
    ("no video formats found", "UNSUPPORTED"),
    ("timed out", "TIMEOUT"),
)


class DownloadError(Exception):
    """`detail` is for logs; `message` (optional) is the user-facing override."""

    def __init__(self, code: str = "FAILED", detail: str = "", message: str = ""):
        self.code = code if code in CATALOG else "FAILED"
        self.detail = detail
        self._message = message
        super().__init__(self.detail or self.code)

    @property
    def http_status(self) -> int:
        return CATALOG[self.code][0]

    @property
    def message(self) -> str:
        return self._message or CATALOG[self.code][1]


def classify(stderr: str, default: str = "FAILED") -> str:
    """Best-effort mapping of yt-dlp stderr to an error code."""
    text = (stderr or "").lower()
    for needle, code in _SIGNATURES:
        if needle in text:
            return code
    return default

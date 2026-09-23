"""Cache-aside for Gemini and TTS (plan/009 T1.5).

Gemini:  sha256(audio_chunk) + source_lang + target_lang + model -> segments.json
TTS:     sha256(text) + language + voice + engine             -> mp3
Cache dir is separate from WORK_DIR so TTL can differ. TTL + LRU eviction.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

CACHE_DIR = Path(os.getenv("CACHE_DIR") or (Path(__file__).parent / "cache"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", str(7 * 24 * 3600)))  # 7 days
CACHE_MAX_SIZE_MB = int(os.getenv("CACHE_MAX_SIZE_MB", "2048"))  # 2GB
CACHE_MAX_FILES = int(os.getenv("CACHE_MAX_FILES", "5000"))

# Hit/miss counters for /metrics (plan/009 T5.3).
_stats = {"hits": 0, "misses": 0, "writes": 0, "evictions": 0}


def stats() -> dict[str, int]:
    return dict(_stats)


def hit_rate() -> float:
    total = _stats["hits"] + _stats["misses"]
    return round(_stats["hits"] / total, 3) if total else 0.0


def _ensure_cache_dir():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def gemini_cache_key(audio_bytes: bytes, source_lang: str, target_lang: str, model: str) -> str:
    h = hashlib.sha256()
    h.update(audio_bytes)
    h.update(source_lang.encode())
    h.update(target_lang.encode())
    h.update(model.encode())
    return f"gemini_{h.hexdigest()[:16]}.json"


def tts_cache_key(text: str, language: str, voice: str | None, engine: str) -> str:
    h = hashlib.sha256()
    h.update(text.encode("utf-8"))
    h.update(language.encode())
    h.update((voice or "").encode())
    h.update(engine.encode())
    return f"tts_{h.hexdigest()[:16]}.mp3"


def get_cache_path(key: str) -> Path:
    _ensure_cache_dir()
    return CACHE_DIR / key


def is_cached(key: str) -> bool:
    p = CACHE_DIR / key
    if not p.exists():
        return False
    # TTL check
    if time.time() - p.stat().st_mtime > CACHE_TTL_SECONDS:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
        return False
    return True


def put_cache(key: str, data: bytes):
    _ensure_cache_dir()
    p = CACHE_DIR / key
    tmp = Path(str(p) + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(p)
    _stats["writes"] += 1
    _evict_if_needed()


def get_cache(key: str) -> bytes | None:
    p = CACHE_DIR / key
    if not is_cached(key):
        _stats["misses"] += 1
        return None
    try:
        # touch to update LRU (atime)
        p.touch(exist_ok=True)
        _stats["hits"] += 1
        return p.read_bytes()
    except Exception:
        _stats["misses"] += 1
        return None


def _evict_if_needed():
    try:
        files = [
            (p, p.stat().st_mtime, p.stat().st_size) for p in CACHE_DIR.glob("*") if p.is_file()
        ]
    except Exception:
        return
    total_size = sum(s for _, _, s in files)
    max_bytes = CACHE_MAX_SIZE_MB * 1024 * 1024
    if len(files) <= CACHE_MAX_FILES and total_size <= max_bytes:
        return
    # LRU: oldest first
    files.sort(key=lambda x: x[1])
    for p, _, s in files:
        try:
            p.unlink(missing_ok=True)
            _stats["evictions"] += 1
            total_size -= s
            if len(list(CACHE_DIR.glob("*"))) <= CACHE_MAX_FILES and total_size <= max_bytes:
                break
        except Exception:
            continue

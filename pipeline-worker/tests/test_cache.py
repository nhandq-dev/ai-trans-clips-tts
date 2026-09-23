"""Tests for cache-aside helpers (plan/009 T1.5)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _fresh_cache(tmp_path, monkeypatch):
    import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(cache, "CACHE_TTL_SECONDS", 3600)
    monkeypatch.setattr(cache, "CACHE_MAX_SIZE_MB", 1)
    monkeypatch.setattr(cache, "CACHE_MAX_FILES", 3)
    return cache


def test_gemini_key_is_stable_and_sensitive():
    import cache

    a = cache.gemini_cache_key(b"audio", "en", "vi", "gemini-3.7-flash")
    b = cache.gemini_cache_key(b"audio", "en", "vi", "gemini-3.7-flash")
    c = cache.gemini_cache_key(b"audio", "en", "ja", "gemini-3.7-flash")
    d = cache.gemini_cache_key(b"other", "en", "vi", "gemini-3.7-flash")
    assert a == b
    assert a != c
    assert a != d


def test_tts_key_varies_by_voice_and_engine():
    import cache

    a = cache.tts_cache_key("hi", "vi", "Minh Đức", "vieneu")
    b = cache.tts_cache_key("hi", "vi", "Minh Đức", "vieneu")
    c = cache.tts_cache_key("hi", "vi", "Other", "vieneu")
    d = cache.tts_cache_key("hi", "vi", "Minh Đức", "edge-tts")
    assert a == b
    assert a != c
    assert a != d


def test_put_get_roundtrip(tmp_path, monkeypatch):
    cache = _fresh_cache(tmp_path, monkeypatch)
    assert cache.get_cache("k1") is None
    cache.put_cache("k1", b"hello")
    assert cache.get_cache("k1") == b"hello"


def test_ttl_expiry(tmp_path, monkeypatch):
    cache = _fresh_cache(tmp_path, monkeypatch)
    cache.put_cache("k1", b"data")
    p = tmp_path / "k1"
    old = time.time() - 7200
    os.utime(p, (old, old))
    assert cache.get_cache("k1") is None
    assert not p.exists()


def test_eviction_by_file_count(tmp_path, monkeypatch):
    cache = _fresh_cache(tmp_path, monkeypatch)
    # CACHE_MAX_FILES=3 -> a 4th write evicts the oldest
    for i in range(4):
        cache.put_cache(f"f{i}", b"x")
        p = tmp_path / f"f{i}"
        t = time.time() + i
        os.utime(p, (t, t))
    cache.put_cache("f4", b"x")
    files = sorted(p.name for p in tmp_path.glob("*") if p.is_file())
    assert len(files) <= 3
    assert "f0" not in files  # oldest evicted

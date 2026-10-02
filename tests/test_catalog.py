"""Catalog consistency after the edge-tts -> Kokoro-82M migration.

The catalog is the single source of truth for `LANGUAGES` and `VOICES`; these
tests guard against drift (a language without voices, a voice pointing at a
language that is not exposed, or a stale engine label).
"""

from __future__ import annotations

from app.services.voice_catalog import (
    KOKORO,
    LANGUAGES,
    VIENEU,
    VOICES,
    is_valid_voice,
    voices_for_language,
)

EXPECTED_LANGUAGES = ["vi", "en", "zh", "ja", "es", "fr", "hi", "it", "pt"]
EXPECTED_VOICE_COUNTS = {
    "vi": 20,
    "en": 28,
    "zh": 8,
    "ja": 5,
    "es": 3,
    "fr": 1,
    "hi": 4,
    "it": 2,
    "pt": 3,
}


def test_language_list_is_exactly_vieneu_plus_kokoro():
    codes = [entry["code"] for entry in LANGUAGES]
    assert codes == EXPECTED_LANGUAGES
    engines = {entry["code"]: entry["engine"] for entry in LANGUAGES}
    assert engines["vi"] == VIENEU
    assert all(engines[code] == KOKORO for code in EXPECTED_LANGUAGES[1:])


def test_every_language_has_at_least_one_voice():
    for entry in LANGUAGES:
        assert voices_for_language(entry["code"]), entry["code"]


def test_every_voice_belongs_to_an_exposed_language_and_known_engine():
    codes = {entry["code"] for entry in LANGUAGES}
    for voice in VOICES:
        assert voice["language"] in codes
        assert voice["engine"] in (VIENEU, KOKORO)
        assert voice["engine"] != "edge-tts"


def test_voice_counts_match_kokoro_pack():
    assert len(VOICES) == sum(EXPECTED_VOICE_COUNTS.values()) == 74
    for language, expected in EXPECTED_VOICE_COUNTS.items():
        assert len(voices_for_language(language)) == expected, language


def test_edge_voice_is_removed():
    assert not is_valid_voice("en-US-JennyNeural", "en")
    assert is_valid_voice("af_heart", "en")
    assert is_valid_voice("zf_xiaoxiao", "zh")
    assert is_valid_voice("jf_alpha", "ja")
    assert is_valid_voice("ff_siwis", "fr")
    # Korean and German are no longer offered.
    assert not is_valid_voice("ko-KR-SunHiNeural", "ko")
    assert not is_valid_voice("de-DE-KatjaNeural", "de")


def test_hindi_voices_match_canonical_pack():
    hindi = {voice["id"] for voice in voices_for_language("hi")}
    # The v1.0 voice pack ships `hf_beta`, not `hf_bella`.
    assert {"hf_alpha", "hf_beta", "hm_omega", "hm_psi"} <= hindi

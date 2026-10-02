from __future__ import annotations

VIENEU = "vieneu"
KOKORO = "kokoro"

# Default Kokoro voice per app language. Voice ids follow Kokoro-82M `VOICES.md`
# (hexgrad) and are backed by `voices-v1.0.bin` in kokoro-onnx.
KOKORO_DEFAULT_VOICES = {
    "en": "af_heart",
    "zh": "zf_xiaoxiao",
    "ja": "jf_alpha",
    "es": "ef_dora",
    "fr": "ff_siwis",
    "hi": "hf_alpha",
    "it": "if_sara",
    "pt": "pf_dora",
}

# App language -> the `lang` argument Kokoro.create() passes to the espeak-ng G2P.
KOKORO_LANG = {
    "en": "en-us",
    "zh": "cmn",
    "ja": "ja",
    "es": "es",
    "fr": "fr-fr",
    "hi": "hi",
    "it": "it",
    "pt": "pt-br",
}


def lang_code(language: str) -> str:
    return (language or "").strip().lower().replace("_", "-").split("-")[0]


def is_vietnamese(language: str) -> bool:
    return lang_code(language) == "vi"


def engine_for(language: str) -> str:
    return VIENEU if is_vietnamese(language) else KOKORO


def kokoro_lang_for(language: str) -> str:
    return KOKORO_LANG.get(lang_code(language), "en-us")


def default_voice_for(language: str) -> str:
    return KOKORO_DEFAULT_VOICES.get(lang_code(language), "af_heart")

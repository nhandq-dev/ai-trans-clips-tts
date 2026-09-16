from __future__ import annotations

EDGE_DEFAULT_VOICES = {
    "vi": "vi-VN-HoaiMyNeural",
    "en": "en-US-JennyNeural",
    "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "fr": "fr-FR-DeniseNeural",
    "es": "es-ES-ElviraNeural",
    "de": "de-DE-KatjaNeural",
    "pt": "pt-BR-FranciscaNeural",
    "it": "it-IT-ElsaNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "th": "th-TH-PremwadeeNeural",
    "id": "id-ID-GadisNeural",
    "hi": "hi-IN-SwaraNeural",
    "ar": "ar-EG-SalmaNeural",
}


def lang_code(language: str) -> str:
    return (language or "").strip().lower().replace("_", "-").split("-")[0]


def is_vietnamese(language: str) -> bool:
    return lang_code(language) == "vi"


def engine_for(language: str) -> str:
    return "vieneu" if is_vietnamese(language) else "edge-tts"

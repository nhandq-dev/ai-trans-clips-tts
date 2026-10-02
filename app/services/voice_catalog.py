from __future__ import annotations

VIENEU = "vieneu"
KOKORO = "kokoro"

# Single source of truth for the exposed languages. "vi" runs on VieNeu, every
# other language runs on Kokoro-82M (via kokoro-onnx). Language codes and locale
# values match the Kokoro-82M `voices-v1.0.bin` catalog (54 preset voices).
_LANG_META = [
    ("vi", "Vietnamese", "🇻🇳", VIENEU),
    ("en", "English", "🇺🇸", KOKORO),
    ("zh", "Chinese", "🇨🇳", KOKORO),
    ("ja", "Japanese", "🇯🇵", KOKORO),
    ("es", "Spanish", "🇪🇸", KOKORO),
    ("fr", "French", "🇫🇷", KOKORO),
    ("hi", "Hindi", "🇮🇳", KOKORO),
    ("it", "Italian", "🇮🇹", KOKORO),
    ("pt", "Portuguese (Brazil)", "🇧🇷", KOKORO),
]

LANGUAGES = [
    {"code": code, "label": label, "flag": flag, "engine": engine}
    for code, label, flag, engine in _LANG_META
]

# VieNeu preset voices (Vietnamese) — id/name/locale come from the VieNeu model.
_VIENEU_VOICES = [
    # male
    ("Minh Đức", "Minh Đức", "male", "vi-VN"),
    ("Phạm Tuyên", "Phạm Tuyên", "male", "vi-VN"),
    ("Thái Sơn", "Thái Sơn", "male", "vi-VN"),
    ("Xuân Vĩnh", "Xuân Vĩnh", "male", "vi-VN"),
    ("Thanh Bình", "Thanh Bình", "male", "vi-VN"),
    ("Minh Triết", "Minh Triết", "male", "vi-VN"),
    ("Quang Sơn", "Quang Sơn", "male", "vi-VN"),
    ("Đức Trí", "Đức Trí", "male", "vi-VN"),
    ("Adam", "Adam", "male", "vi-VN"),
    # female
    ("Trúc Ly", "Trúc Ly", "female", "vi-VN"),
    ("Ngọc Linh", "Ngọc Linh", "female", "vi-VN"),
    ("Đoan Trang", "Đoan Trang", "female", "vi-VN"),
    ("Mai Anh", "Mai Anh", "female", "vi-VN"),
    ("Thục Đoan", "Thục Đoan", "female", "vi-VN"),
    ("Thùy Dung", "Thùy Dung", "female", "vi-VN"),
    ("Ngọc Trân", "Ngọc Trân", "female", "vi-VN"),
    ("Mỹ Duyên", "Mỹ Duyên", "female", "vi-VN"),
    ("Quỳnh Anh", "Quỳnh Anh", "female", "vi-VN"),
    ("Kim Thanh", "Kim Thanh", "female", "vi-VN"),
    ("Ngọc Huyền", "Ngọc Huyền", "female", "vi-VN"),
]

# Kokoro-82M preset voices: (id, name, gender, locale, language). Mirrors the
# 54 voices shipped in `voices-v1.0.bin` exactly (verified against the model).
# NOTE: Hindi female voices are `hf_alpha` + `hf_beta` in the v1.0 voice pack.
_KOKORO_VOICES = [
    # 🇺🇸 American English
    ("af_alloy", "Alloy", "female", "en-US", "en"),
    ("af_aoede", "Aoede", "female", "en-US", "en"),
    ("af_bella", "Bella", "female", "en-US", "en"),
    ("af_heart", "Heart", "female", "en-US", "en"),
    ("af_jessica", "Jessica", "female", "en-US", "en"),
    ("af_kore", "Kore", "female", "en-US", "en"),
    ("af_nicole", "Nicole", "female", "en-US", "en"),
    ("af_nova", "Nova", "female", "en-US", "en"),
    ("af_river", "River", "female", "en-US", "en"),
    ("af_sarah", "Sarah", "female", "en-US", "en"),
    ("af_sky", "Sky", "female", "en-US", "en"),
    ("am_adam", "Adam", "male", "en-US", "en"),
    ("am_echo", "Echo", "male", "en-US", "en"),
    ("am_eric", "Eric", "male", "en-US", "en"),
    ("am_fenrir", "Fenrir", "male", "en-US", "en"),
    ("am_liam", "Liam", "male", "en-US", "en"),
    ("am_michael", "Michael", "male", "en-US", "en"),
    ("am_onyx", "Onyx", "male", "en-US", "en"),
    ("am_puck", "Puck", "male", "en-US", "en"),
    ("am_santa", "Santa", "male", "en-US", "en"),
    # 🇬🇧 British English
    ("bf_alice", "Alice", "female", "en-GB", "en"),
    ("bf_emma", "Emma", "female", "en-GB", "en"),
    ("bf_isabella", "Isabella", "female", "en-GB", "en"),
    ("bf_lily", "Lily", "female", "en-GB", "en"),
    ("bm_daniel", "Daniel", "male", "en-GB", "en"),
    ("bm_fable", "Fable", "male", "en-GB", "en"),
    ("bm_george", "George", "male", "en-GB", "en"),
    ("bm_lewis", "Lewis", "male", "en-GB", "en"),
    # 🇨🇳 Mandarin Chinese
    ("zf_xiaobei", "Xiaobei", "female", "zh-CN", "zh"),
    ("zf_xiaoni", "Xiaoni", "female", "zh-CN", "zh"),
    ("zf_xiaoxiao", "Xiaoxiao", "female", "zh-CN", "zh"),
    ("zf_xiaoyi", "Xiaoyi", "female", "zh-CN", "zh"),
    ("zm_yunjian", "Yunjian", "male", "zh-CN", "zh"),
    ("zm_yunxi", "Yunxi", "male", "zh-CN", "zh"),
    ("zm_yunxia", "Yunxia", "male", "zh-CN", "zh"),
    ("zm_yunyang", "Yunyang", "male", "zh-CN", "zh"),
    # 🇯🇵 Japanese
    ("jf_alpha", "Alpha", "female", "ja-JP", "ja"),
    ("jf_gongitsune", "Gongitsune", "female", "ja-JP", "ja"),
    ("jf_nezumi", "Nezumi", "female", "ja-JP", "ja"),
    ("jf_tebukuro", "Tebukuro", "female", "ja-JP", "ja"),
    ("jm_kumo", "Kumo", "male", "ja-JP", "ja"),
    # 🇪🇸 Spanish
    ("ef_dora", "Dora", "female", "es-ES", "es"),
    ("em_alex", "Alex", "male", "es-ES", "es"),
    ("em_santa", "Santa", "male", "es-ES", "es"),
    # 🇫🇷 French
    ("ff_siwis", "Siwis", "female", "fr-FR", "fr"),
    # 🇮🇳 Hindi
    ("hf_alpha", "Alpha", "female", "hi-IN", "hi"),
    ("hf_beta", "Beta", "female", "hi-IN", "hi"),
    ("hm_omega", "Omega", "male", "hi-IN", "hi"),
    ("hm_psi", "Psi", "male", "hi-IN", "hi"),
    # 🇮🇹 Italian
    ("if_sara", "Sara", "female", "it-IT", "it"),
    ("im_nicola", "Nicola", "male", "it-IT", "it"),
    # 🇧🇷 Brazilian Portuguese
    ("pf_dora", "Dora", "female", "pt-BR", "pt"),
    ("pm_alex", "Alex", "male", "pt-BR", "pt"),
    ("pm_santa", "Santa", "male", "pt-BR", "pt"),
]

VOICES = [
    {
        "id": voice_id,
        "name": name,
        "gender": gender,
        "locale": locale,
        "language": "vi",
        "engine": VIENEU,
    }
    for voice_id, name, gender, locale in _VIENEU_VOICES
] + [
    {
        "id": voice_id,
        "name": name,
        "gender": gender,
        "locale": locale,
        "language": language,
        "engine": KOKORO,
    }
    for voice_id, name, gender, locale, language in _KOKORO_VOICES
]

GREETINGS = {
    "vi": "Xin chào, chào mừng bạn đến với AI Trans Clips, chúc bạn có một ngày vui vẻ?",
    "en": "Hello, welcome to AI Trans Clips. Have a wonderful day!",
    "zh": "你好，欢迎来到 AI Trans Clips，祝你今天愉快！",
    "ja": "こんにちは、AI Trans Clips へようこそ。良い一日をお過ごしください！",
    "es": "Hola, bienvenido a AI Trans Clips. ¡Que tengas un buen día!",
    "fr": "Bonjour, bienvenue sur AI Trans Clips. Passez une excellente journée !",
    "hi": "नमस्ते, AI Trans Clips में आपका स्वागत है। आपका दिन शुभ हो!",
    "it": "Ciao, benvenuto su AI Trans Clips. Buona giornata!",
    "pt": "Olá, bem-vindo ao AI Trans Clips. Tenha um ótimo dia!",
}


def greeting_for(language: str) -> str:
    return GREETINGS.get(language, GREETINGS["en"])


def voices_for_language(language: str) -> list[dict]:
    return [voice for voice in VOICES if voice["language"] == language]


LANGUAGE_CODES = frozenset(entry["code"] for entry in LANGUAGES)


def is_supported_language(language: str) -> bool:
    """Whether `language` is one of the languages exposed by the catalog."""
    return language in LANGUAGE_CODES


def is_valid_voice(voice: str, language: str) -> bool:
    """Whether `voice` belongs to `language` for its engine."""
    return any(v["id"] == voice and v["language"] == language for v in VOICES)


def catalog() -> dict:
    """Voice catalog plus the effective text limits.

    Clients must read the limits from here instead of hardcoding them: both are
    worker capabilities, not business limits. Per-plan character caps live in the
    API (`tts_max_chars_per_request`). `syncMaxTextLength` is what one blocking
    request can finish; anything above it must go through the async job endpoints,
    which are bounded by `asyncMaxTextLength`.
    """
    from app.core.config import get_settings

    settings = get_settings()
    return {
        "languages": LANGUAGES,
        "voices": VOICES,
        "limits": {
            "syncMaxTextLength": settings.sync_max_text_length,
            "asyncMaxTextLength": settings.async_max_text_length,
        },
    }

"""Kokoro synthesis plumbing (mirror of `tests/test_synthesis_voice.py`).

`_synth_kokoro` must resolve the voice, chunk the text, render one WAV per chunk
through the kokoro service, and merge them through ffmpeg — with a fake service
so the test never touches the ONNX model or espeak-ng.
"""

from __future__ import annotations

from pathlib import Path

from app.services.engine_router import engine_for, kokoro_lang_for
from app.services.synthesis import _resolve_kokoro_voice, _synth_kokoro


def test_engine_routing():
    assert engine_for("vi") == "vieneu"
    assert engine_for("vi-VN") == "vieneu"
    assert engine_for("en") == "kokoro"
    assert engine_for("ja") == "kokoro"


def test_kokoro_g2p_lang_mapping():
    # Exact espeak-ng backend codes verified against kokoro-onnx 0.6.1.
    assert kokoro_lang_for("en") == "en-us"
    assert kokoro_lang_for("zh") == "cmn"
    assert kokoro_lang_for("ja") == "ja"
    assert kokoro_lang_for("es") == "es"
    assert kokoro_lang_for("fr") == "fr-fr"
    assert kokoro_lang_for("hi") == "hi"
    assert kokoro_lang_for("it") == "it"
    assert kokoro_lang_for("pt") == "pt-br"


def test_default_voice_per_language():
    assert _resolve_kokoro_voice("en", None) == "af_heart"
    assert _resolve_kokoro_voice("zh", "zm_yunxi") == "zm_yunxi"


def test_synth_kokoro_writes_merged_wav(monkeypatch, tmp_path):
    from app.services import kokoro as kokoro_svc
    from app.services import synthesis

    written: list[Path] = []

    def fake_synth(language, text, voice, dest):  # noqa: ANN001
        dest = Path(dest)
        written.append(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"RIFF")

    monkeypatch.setattr(kokoro_svc, "synth_to_wav", fake_synth)
    monkeypatch.setattr(synthesis, "split_text", lambda text, limit: [text])

    dest = tmp_path / "out.wav"
    _synth_kokoro("en", "Hello world.", None, dest, "wav", tmp_path, None)

    assert dest.exists()
    assert dest.read_bytes() == b"RIFF"
    assert len(written) == 1

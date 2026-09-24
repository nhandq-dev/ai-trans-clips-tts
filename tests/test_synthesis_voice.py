"""Custom-voice plumbing (regression for the "clone sounds like Adam" bug).

VieNeu's ``infer(voice=...)`` accepts a preset *name* or a preset *dict*.
``encode_reference`` returns ``(speaker_emb, ref_codes)``; passing that tuple
straight through matched neither branch in ``_resolve_ref``, so VieNeu silently
fell back to its default preset — a cloned voice came out as the built-in default.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.services.synthesis import to_vieneu_voice_arg


def test_encoded_reference_is_wrapped_as_a_preset_dict():
    speaker_emb = np.zeros(8, dtype=np.float32)
    ref_codes = np.zeros(4, dtype=np.int64)

    arg = to_vieneu_voice_arg((speaker_emb, ref_codes))

    assert isinstance(arg, dict), "a tuple would be ignored by infer(voice=...)"
    assert set(arg) == {"speaker_emb", "codes"}
    assert arg["speaker_emb"].dtype == np.float32
    assert arg["codes"].dtype == np.int64


def test_none_voice_data_stays_none():
    assert to_vieneu_voice_arg(None) is None


def test_missing_codes_are_preserved_as_none():
    arg = to_vieneu_voice_arg((np.zeros(8, dtype=np.float32), None))
    assert arg is not None
    assert arg["codes"] is None


def test_infer_receives_a_dict_not_a_tuple(monkeypatch):
    """The whole point: what reaches `model.infer` must be usable as a preset."""
    from pathlib import Path

    from app.services import synthesis

    captured: dict = {}

    class _FakeModel:
        def infer(self, *, text, voice, **kwargs):  # noqa: ANN001
            captured["voice"] = voice
            return np.zeros(16, dtype=np.float32)

        def save(self, audio, path):  # noqa: ANN001, ARG002
            Path(path).write_bytes(b"RIFF")

    monkeypatch.setattr(synthesis, "_get_model", lambda: _FakeModel())
    # `merge` is irrelevant here; keep the chunk list to one item so it runs once.
    monkeypatch.setattr(synthesis, "split_text", lambda text, limit: [text])

    out = Path("/tmp/voice_arg_check.wav")
    synthesis._synth_vieneu(
        "xin chào",
        None,
        out,
        "wav",
        Path("/tmp"),
        None,
        (np.zeros(8, dtype=np.float32), np.zeros(4, dtype=np.int64)),
    )

    voice = captured["voice"]
    assert isinstance(voice, dict)
    assert "speaker_emb" in voice
    assert pytest.approx(voice["speaker_emb"].shape) == (8,)

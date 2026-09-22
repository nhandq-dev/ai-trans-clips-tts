"""Transcript helpers — markdown table + SRT (plan/009 T1.4)."""

from __future__ import annotations

import datetime
from pathlib import Path

import srt
from schemas import TranscriptionResult


def _escape_md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", "<br>")


def _fmt_time(seconds: float) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(int(m), 60)
    return f"{h:02d}:{int(m):02d}:{s:06.3f}" if h else f"{int(m):02d}:{s:06.3f}"


def to_markdown(result: TranscriptionResult, title: str | None = None) -> str:
    lines = []
    if title:
        lines.append(f"# {title}\n")
    lines.append(f"_Detected: {result.detected_language} — {len(result.segments)} segments_\n")
    lines.append("| # | Time | Source | Translation |")
    lines.append("|---|---|---|---|")
    for i, seg in enumerate(result.segments, 1):
        t = f"{_fmt_time(seg.start)} → {_fmt_time(seg.end)}"
        lines.append(
            f"| {i} | {t} | {_escape_md(seg.source_text)} | {_escape_md(seg.target_text)} |"
        )
    lines.append("")
    return "\n".join(lines)


def to_srt(result: TranscriptionResult, *, target: bool = True) -> str:
    """SRT from target_text (default) or source_text. Uses srt library."""
    subs = []
    for idx, seg in enumerate(result.segments, 1):
        text = seg.target_text if target else seg.source_text
        subs.append(
            srt.Subtitle(
                index=idx,
                start=datetime.timedelta(seconds=seg.start),
                end=datetime.timedelta(seconds=seg.end),
                content=text,
            )
        )
    return srt.compose(subs)


def write_outputs(
    result: TranscriptionResult, out_dir: str | Path, title: str | None = None
) -> dict[str, Path]:
    """Write transcript.md, transcript.srt, transcript.json, segments.json atomically."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    md = to_markdown(result, title)
    srt_text = to_srt(result, target=True)
    json_text = result.model_dump_json(indent=2)

    files: dict[str, Path] = {}
    for name, content in [
        ("transcript.md", md),
        ("transcript.srt", srt_text),
        ("segments.json", json_text),
    ]:
        p = out_dir / name
        tmp = Path(str(p) + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(p)
        files[name] = p
    return files

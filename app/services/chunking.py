from __future__ import annotations

import re


def split_text(text: str, max_chars: int) -> list[str]:
    """Split text into sentence-aware chunks no larger than `max_chars`."""
    sents = re.split(r"(?<=[.!?。！？\n])\s*", text)
    sents = [s.strip() for s in sents if s.strip()]
    chunks: list[str] = []
    cur = ""
    for s in sents:
        if len(cur) + len(s) + 1 > max_chars and cur:
            chunks.append(cur)
            cur = s
        else:
            cur = (cur + " " + s) if cur else s
    if cur:
        chunks.append(cur)
    return chunks or [text]

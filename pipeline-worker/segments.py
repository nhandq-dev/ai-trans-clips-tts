"""Segment post-processing for Gemini output (plan/009 T1.2).

Gemini returns ``segments`` with start/end in seconds. We normalise:
- sort by start
- drop empty source/target
- merge very short segments (<0.4s) into neighbours (so TTS is not a blip)
"""

from __future__ import annotations

from schemas import Segment


def normalize_segments(segments: list[Segment], *, min_duration: float = 0.4) -> list[Segment]:
    """Sort, drop empty, merge short segments. Returns a new list."""
    # drop empty
    segs = [s for s in segments if s.source_text.strip() and s.target_text.strip()]
    if not segs:
        return []
    # sort
    segs.sort(key=lambda s: s.start)
    # merge short segments into next (or previous if last)
    merged: list[Segment] = []
    i = 0
    while i < len(segs):
        cur = segs[i]
        dur = cur.end - cur.start
        if dur >= min_duration or len(segs) == 1:
            merged.append(cur)
            i += 1
            continue
        # short segment: try merge into next
        if i + 1 < len(segs):
            nxt = segs[i + 1]
            merged_seg = Segment(
                start=cur.start,
                end=nxt.end,
                source_text=f"{cur.source_text} {nxt.source_text}".strip(),
                target_text=f"{cur.target_text} {nxt.target_text}".strip(),
            )
            # replace next with merged, skip cur
            segs[i + 1] = merged_seg
            i += 1
        else:
            # last and short: merge into previous
            if merged:
                prev = merged.pop()
                merged.append(
                    Segment(
                        start=prev.start,
                        end=cur.end,
                        source_text=f"{prev.source_text} {cur.source_text}".strip(),
                        target_text=f"{prev.target_text} {cur.target_text}".strip(),
                    )
                )
            else:
                merged.append(cur)
            i += 1
    return merged

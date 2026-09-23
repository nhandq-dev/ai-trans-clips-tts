from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

from app.core.config import get_settings


def ffmpeg_bin() -> str:
    return get_settings().ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        # The useful part of ffmpeg's stderr is at the end; the banner is first.
        detail = proc.stderr.decode(errors="replace")
        raise RuntimeError(detail[-800:])


def transcode(src: Path, dest: Path) -> None:
    run([ffmpeg_bin(), "-y", "-i", str(src), "-b:a", "128k", str(dest)])


def concat(chunks: list[Path], dest: Path, workdir: Path) -> None:
    list_file = workdir / f"concat_{uuid.uuid4().hex}.txt"
    list_file.write_text("\n".join(f"file '{c.resolve()}'" for c in chunks), encoding="utf-8")
    try:
        run(
            [
                ffmpeg_bin(),
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-b:a",
                "128k",
                str(dest),
            ]
        )
    finally:
        list_file.unlink(missing_ok=True)


def merge(chunks: list[Path], dest: Path, src_ext: str, fmt: str, workdir: Path) -> None:
    """Move or transcode a single chunk, otherwise concatenate all chunks into `dest`."""
    if len(chunks) == 1:
        src = chunks[0]
        if src_ext == fmt:
            shutil.move(str(src), str(dest))
        else:
            transcode(src, dest)
            src.unlink(missing_ok=True)
        return
    concat(chunks, dest, workdir)
    for chunk in chunks:
        chunk.unlink(missing_ok=True)


def output_dir() -> Path:
    return get_settings().tts_output_dir


def make_workdir(parent: Path) -> Path:
    workdir = parent / f".tmp_{uuid.uuid4().hex}"
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def cleanup_workdir(workdir: Path) -> None:
    shutil.rmtree(workdir, ignore_errors=True)

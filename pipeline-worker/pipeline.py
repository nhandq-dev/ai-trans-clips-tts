"""Orchestrator — runs stages sequentially with resume (plan/009 T2.4)."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import jobs
from errors import MUX_FAILED
from media import extract_audio, probe
from transcript import write_outputs

WORK_DIR = Path(os.getenv("WORK_DIR") or (Path(__file__).parent / "output"))
CACHE_DIR = Path(os.getenv("CACHE_DIR") or (Path(__file__).parent / "cache"))

# stage progress map (plan/009 §4.2)
STAGE_PROGRESS = {
    "downloading": 15,
    "extracting": 22,
    "transcribing": 45,
    "synthesizing": 75,
    "aligning": 82,
    "muxing": 98,
    "done": 100,
}


async def _update(job_id: str, stage: str, progress: int | None = None, **fields):
    if progress is None:
        progress = STAGE_PROGRESS.get(stage, 0)
    await jobs.update_job(job_id, stage=stage, progress=progress, **fields)


async def run_pipeline(job_id: str):
    """Run all stages for job_id. Called as background task from main.py."""
    job = await jobs.get_job(job_id)
    if not job:
        return
    # check cancel before start
    if job.status == "canceled":
        return
    try:
        # ------------------------------------------------------------------
        # Stage: downloading (or source_path local)
        # ------------------------------------------------------------------
        await _update(job_id, "downloading")
        job = await jobs.get_job(job_id)
        assert job is not None
        work = WORK_DIR / job_id
        work.mkdir(parents=True, exist_ok=True)
        source_mp4 = work / "source.mp4"
        # resume: if source.mp4 already exists and valid, skip download
        if source_mp4.exists() and source_mp4.stat().st_size > 0 and "downloading" in job.stage_state:
            pass
        else:
            if job.source_key:
                # download from S3 (presign-less, use boto3 download)
                from storage import _client, S3_BUCKET

                client = _client()
                await asyncio.to_thread(client.download_file, S3_BUCKET, job.source_key, str(source_mp4))
            elif job.source_url:
                # call video-worker to download (loopback on VPS, via HMAC)
                # For local dev, we can call video-worker via localhost if available
                # Fallback: try yt-dlp directly? For MVP, require source_path
                raise NotImplementedError("source_url download not yet implemented in scaffold — use source_path")
            elif job.source_url is None and job.source_key is None:
                # For test, source may be a local path passed as source_url=file:///...
                # Try to handle file://
                raise NotImplementedError("no source")
            # mark done
            await jobs.update_job(job_id, stage_state={**job.stage_state, "downloading": "done"})
            job = await jobs.get_job(job_id)

        # ------------------------------------------------------------------
        # Stage: extracting
        # ------------------------------------------------------------------
        if "extracting" not in job.stage_state:
            await _update(job_id, "extracting")
            audio_flac = work / "audio.flac"
            # resume: if audio.flac exists, skip
            if not audio_flac.exists():
                await extract_audio(source_mp4, audio_flac)
            await jobs.update_job(job_id, stage_state={**job.stage_state, "extracting": "done"})
            job = await jobs.get_job(job_id)  # type: ignore
        else:
            audio_flac = work / "audio.flac"

        # ------------------------------------------------------------------
        # Stage: transcribing (chunked)
        # ------------------------------------------------------------------
        if "transcribing" not in job.stage_state:
            await _update(job_id, "transcribing")
            from chunking import transcribe_chunked
            from schemas import TranscriptionResult

            result: TranscriptionResult = await transcribe_chunked(
                audio_flac, job.source_language, job.target_language, work_dir=work / "chunks"
            )
            # write outputs
            write_outputs(result, work)
            await jobs.update_job(job_id, stage_state={**job.stage_state, "transcribing": "done"})
            job = await jobs.get_job(job_id)  # type: ignore
        else:
            # load segments.json
            import json

            from schemas import TranscriptionResult

            seg_path = work / "segments.json"
            if seg_path.exists():
                result = TranscriptionResult.model_validate_json(seg_path.read_text())
            else:
                result = TranscriptionResult(detected_language=job.source_language, segments=[])

        # if no segments, skip TTS/mux but still mark done
        if not result.segments:
            await _update(job_id, "done", 100, artifacts={"video": None, "markdown": str(work / "transcript.md")})
            return

        # ------------------------------------------------------------------
        # Stage: synthesizing (TTS per segment)
        # ------------------------------------------------------------------
        if "synthesizing" not in job.stage_state:
            await _update(job_id, "synthesizing")
            from tts import synthesize_all

            tts_dir = work / "tts"
            # synthesize
            try:
                tts_paths = await synthesize_all(result.segments, job.target_language, job.voice, tts_dir)
            except Exception as exc:
                # partial handling: if some segments failed, we still continue with what we have
                # For now, raise
                raise
            await jobs.update_job(job_id, stage_state={**job.stage_state, "synthesizing": f"done:{len(tts_paths)}/{len(result.segments)}"})
            job = await jobs.get_job(job_id)  # type: ignore
        else:
            tts_dir = work / "tts"
            tts_paths = sorted(tts_dir.glob("seg_*.mp3")) if tts_dir.exists() else []

        if not tts_paths:
            raise RuntimeError("no TTS clips generated")

        # ------------------------------------------------------------------
        # Stage: aligning (fit to slot)
        # ------------------------------------------------------------------
        if "aligning" not in job.stage_state:
            await _update(job_id, "aligning")
            from align import fit_to_slot

            aligned_dir = work / "aligned"
            aligned_dir.mkdir(parents=True, exist_ok=True)
            aligned_clips: list[tuple[Path, float]] = []
            for idx, seg in enumerate(result.segments):
                if idx >= len(tts_paths):
                    break
                src = tts_paths[idx]
                slot = seg.end - seg.start
                dst = aligned_dir / f"seg_{idx:04d}.wav"
                if dst.exists():
                    aligned_clips.append((dst, seg.start))
                    continue
                await fit_to_slot(src, dst, slot)
                aligned_clips.append((dst, seg.start))
            # store aligned list for mux
            # we keep it in work dir
            await jobs.update_job(job_id, stage_state={**job.stage_state, "aligning": "done"})
            job = await jobs.get_job(job_id)  # type: ignore
        else:
            aligned_dir = work / "aligned"
            aligned_clips = []
            for idx, seg in enumerate(result.segments):
                p = aligned_dir / f"seg_{idx:04d}.wav"
                if p.exists():
                    aligned_clips.append((p, seg.start))

        # ------------------------------------------------------------------
        # Stage: muxing (build dub + mix)
        # ------------------------------------------------------------------
        if "muxing" not in job.stage_state:
            await _update(job_id, "muxing")
            from mux import build_dub_track, mix_and_mux, probe_duration

            video_duration = await probe_duration(source_mp4)
            dub_wav = work / "dub.wav"
            if not dub_wav.exists() or "muxing" not in job.stage_state:
                await build_dub_track(aligned_clips, video_duration, dub_wav)
            translated = work / "translated.mp4"
            # mix
            await mix_and_mux(
                source_mp4,
                dub_wav,
                translated,
                original_volume_db=job.options.get("original_audio_volume_db", -20),
                mute_original=job.options.get("mute_original", False),
            )
            # upload to S3 if configured
            s3_key = None
            try:
                from storage import _client, S3_BUCKET, upload_file

                # check if S3 configured
                import os as _os

                if _os.getenv("S3_BUCKET"):
                    key = f"results/{job.user_id or 'anon'}/{job_id}/translated.mp4"
                    await asyncio.to_thread(upload_file, translated, key, "video/mp4")
                    s3_key = key
                    # also upload transcript
                    for name in ("transcript.md", "transcript.srt", "segments.json"):
                        p = work / name
                        if p.exists():
                            k = f"results/{job.user_id or 'anon'}/{job_id}/{name}"
                            await asyncio.to_thread(upload_file, p, k, "text/plain")
            except Exception:
                pass
            artifacts = {"video": s3_key or str(translated), "transcript": str(work / "transcript.md")}
            await jobs.update_job(
                job_id,
                stage="done",
                progress=100,
                status="completed",
                stage_state={**job.stage_state, "muxing": "done", "done": "done"},
                artifacts=artifacts,
                file_path=str(translated),
                file_name="translated.mp4",
                file_size=translated.stat().st_size if translated.exists() else None,
            )
            return
        # if already done, just mark completed
        await _update(job_id, "done", 100, status="completed")

    except asyncio.CancelledError:
        await jobs.update_job(job_id, status="canceled", error={"code": "CANCELED", "message": "canceled"})
        raise
    except Exception as exc:
        code = getattr(exc, "code", "FAILED")
        msg = getattr(exc, "message", str(exc))
        await jobs.update_job(job_id, status="failed", error={"code": code, "message": msg}, progress=100)
        raise

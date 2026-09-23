"""Orchestrator — runs stages sequentially with resume (plan/009 T2.4)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from pathlib import Path

import httpx
import jobs
from errors import PermanentError
from media import extract_audio, probe
from transcript import write_outputs

logger = logging.getLogger("pipeline-worker")

WORK_DIR = Path(os.getenv("WORK_DIR") or (Path(__file__).parent / "output"))
CACHE_DIR = Path(os.getenv("CACHE_DIR") or (Path(__file__).parent / "cache"))

# stage progress map (plan/009 §4.2)
STAGE_PROGRESS = {
    "downloading": 15,
    "extracting": 22,
    "transcribing": 45,
    "detecting_subs": 52,
    "synthesizing": 75,
    "aligning": 82,
    "muxing": 98,
    "done": 100,
}

VIDEO_WORKER_URL = os.getenv("VIDEO_WORKER_URL", "http://127.0.0.1:8005").rstrip("/")
_HMAC_JSON = os.getenv("HMAC_KEYS_JSON", "")


def _pick_video_key() -> tuple[str, str]:
    try:
        keys = json.loads(_HMAC_JSON) if _HMAC_JSON else {}
    except Exception:
        keys = {}
    if "pipeline-key-1" in keys:
        return "pipeline-key-1", keys["pipeline-key-1"]
    if "video-key-1" in keys:
        return "video-key-1", keys["video-key-1"]
    if keys:
        k, v = next(iter(keys.items()))
        return k, v
    return "", ""


def _video_hmac_headers(method: str, path: str, body: bytes) -> dict[str, str]:
    kid, sec = _pick_video_key()
    if not kid:
        return {}
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    bh = hashlib.sha256(body).hexdigest()
    canon = "\n".join([method.upper(), path, ts, nonce, bh])
    sig = hmac.new(sec.encode(), canon.encode(), hashlib.sha256).hexdigest()
    return {
        "X-Video-Key-Id": kid,
        "X-Video-Timestamp": ts,
        "X-Video-Nonce": nonce,
        "X-Video-Content-SHA256": bh,
        "X-Video-Signature": sig,
    }


async def _download_via_video_worker(url: str, dest: Path):
    """Call video-worker POST /jobs, poll, then download the result file."""
    body = json.dumps({"url": url}).encode()
    headers = _video_hmac_headers("POST", "/jobs", body)
    headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as client:
        r = await client.post(f"{VIDEO_WORKER_URL}/jobs", content=body, headers=headers)
        if r.status_code not in (200, 202):
            raise PermanentError(
                "DOWNLOAD_FAILED", f"video-worker create job {r.status_code}: {r.text[:500]}"
            )
        jid = r.json().get("job_id")
        if not jid:
            raise PermanentError("DOWNLOAD_FAILED", "video-worker did not return job_id")
        # poll
        for _ in range(120):
            await asyncio.sleep(3)
            bh = hashlib.sha256(b"").hexdigest()
            ts = str(int(time.time()))
            nonce = uuid.uuid4().hex
            canon = "\n".join(["GET", f"/jobs/{jid}", ts, nonce, bh])
            _, sec = _pick_video_key()
            sig = hmac.new(sec.encode(), canon.encode(), hashlib.sha256).hexdigest() if sec else ""
            kid, _ = _pick_video_key()
            hdrs = {
                "X-Video-Key-Id": kid,
                "X-Video-Timestamp": ts,
                "X-Video-Nonce": nonce,
                "X-Video-Content-SHA256": bh,
                "X-Video-Signature": sig,
            }
            r2 = await client.get(f"{VIDEO_WORKER_URL}/jobs/{jid}", headers=hdrs)
            if r2.status_code != 200:
                continue
            js = r2.json()
            status = js.get("status")
            if status == "failed":
                err = js.get("error") or {}
                raise PermanentError(
                    err.get("code", "DOWNLOAD_FAILED"), err.get("message", "video download failed")
                )
            if status == "completed":
                # try presigned first, fallback to file stream
                # try presign
                bh2 = hashlib.sha256(b"").hexdigest()
                ts2 = str(int(time.time()))
                nonce2 = uuid.uuid4().hex
                canon2 = "\n".join(["GET", f"/jobs/{jid}/presign", ts2, nonce2, bh2])
                sig2 = (
                    hmac.new(sec.encode(), canon2.encode(), hashlib.sha256).hexdigest()
                    if sec
                    else ""
                )
                hdrs2 = {
                    "X-Video-Key-Id": kid,
                    "X-Video-Timestamp": ts2,
                    "X-Video-Nonce": nonce2,
                    "X-Video-Content-SHA256": bh2,
                    "X-Video-Signature": sig2,
                }
                r3 = await client.get(f"{VIDEO_WORKER_URL}/jobs/{jid}/presign", headers=hdrs2)
                if r3.status_code == 200 and r3.json().get("url"):
                    presigned = r3.json()["url"]
                    # download presigned URL (no HMAC)
                    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as dl:
                        resp = await dl.get(presigned)
                        resp.raise_for_status()
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        tmp = Path(str(dest) + ".tmp")
                        tmp.write_bytes(resp.content)
                        tmp.replace(dest)
                        return
                # fallback: GET /file stream
                bh3 = hashlib.sha256(b"").hexdigest()
                ts3 = str(int(time.time()))
                nonce3 = uuid.uuid4().hex
                canon3 = "\n".join(["GET", f"/jobs/{jid}/file", ts3, nonce3, bh3])
                sig3 = (
                    hmac.new(sec.encode(), canon3.encode(), hashlib.sha256).hexdigest()
                    if sec
                    else ""
                )
                hdrs3 = {
                    "X-Video-Key-Id": kid,
                    "X-Video-Timestamp": ts3,
                    "X-Video-Nonce": nonce3,
                    "X-Video-Content-SHA256": bh3,
                    "X-Video-Signature": sig3,
                }
                r4 = await client.get(f"{VIDEO_WORKER_URL}/jobs/{jid}/file", headers=hdrs3)
                if r4.status_code == 200:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = Path(str(dest) + ".tmp")
                    tmp.write_bytes(r4.content)
                    tmp.replace(dest)
                    return
                raise PermanentError(
                    "DOWNLOAD_FAILED", f"video file not available {r4.status_code}"
                )
        raise PermanentError("DOWNLOAD_FAILED", "video download timed out")


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
        # Flip to `processing` as soon as work actually starts, so the UI never
        # shows a running job as merely "queued" while its stage advances.
        await _update(job_id, "downloading", status="processing")
        job = await jobs.get_job(job_id)
        assert job is not None
        work = WORK_DIR / job_id
        work.mkdir(parents=True, exist_ok=True)
        source_mp4 = work / "source.mp4"
        # resume: if source.mp4 already exists and valid, skip download
        if (
            source_mp4.exists()
            and source_mp4.stat().st_size > 0
            and "downloading" in job.stage_state
        ):
            pass
        else:
            if job.source_key:
                # validate object exists, then download from S3
                from storage import S3_BUCKET, _client

                client = _client()
                # HEAD to validate size
                try:
                    head = await asyncio.to_thread(
                        client.head_object, Bucket=S3_BUCKET, Key=job.source_key
                    )
                    size = head.get("ContentLength", 0)
                    if size == 0:
                        raise PermanentError("UNSUPPORTED", "uploaded file is empty")
                    # optional: check content type? skip
                except client.exceptions.NoSuchKey as exc:
                    raise PermanentError(
                        "DOWNLOAD_FAILED", f"source_key not found: {job.source_key}"
                    ) from exc
                except PermanentError:
                    raise
                except Exception as exc:
                    raise PermanentError("DOWNLOAD_FAILED", f"S3 head failed: {exc}") from exc
                await asyncio.to_thread(
                    client.download_file, S3_BUCKET, job.source_key, str(source_mp4)
                )
                # validate it is a video
                try:
                    info = await probe(source_mp4)
                    if info["duration"] <= 0:
                        raise PermanentError("UNSUPPORTED", "file has no duration")
                except PermanentError:
                    raise
                except Exception as exc:
                    raise PermanentError("UNSUPPORTED", f"not a valid video: {exc}") from exc
            elif job.source_url:
                # handle local file for tests: Path exists or file://
                p = None
                if job.source_url.startswith("file://"):
                    p = Path(job.source_url[7:])
                elif Path(job.source_url).exists():
                    p = Path(job.source_url)
                if p and p.exists():
                    import shutil

                    await asyncio.to_thread(shutil.copy2, p, source_mp4)
                else:
                    # remote URL: delegate to video-worker
                    await _download_via_video_worker(job.source_url, source_mp4)
            else:
                raise PermanentError("DOWNLOAD_FAILED", "no source")
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

            from schemas import TranscriptionResult

            seg_path = work / "segments.json"
            if seg_path.exists():
                result = TranscriptionResult.model_validate_json(seg_path.read_text())
            else:
                result = TranscriptionResult(detected_language=job.source_language, segments=[])

        # if no segments, skip TTS/mux but still mark done
        if not result.segments:
            await _update(
                job_id,
                "done",
                100,
                status="completed",
                artifacts={"video": None, "markdown": str(work / "transcript.md")},
                stage_state={**job.stage_state, "transcribing": "done", "done": "done"},
            )
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
                tts_paths = await synthesize_all(
                    result.segments, job.target_language, job.voice, tts_dir
                )
            except Exception:
                # partial handling: if some segments failed, we still continue with what we have
                # For now, raise
                raise
            await jobs.update_job(
                job_id,
                stage_state={
                    **job.stage_state,
                    "synthesizing": f"done:{len(tts_paths)}/{len(result.segments)}",
                },
            )
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
            from mux import build_dub_track, probe_duration, render_translated_video

            video_duration = await probe_duration(source_mp4)
            dub_wav = work / "dub.wav"
            if not dub_wav.exists():
                await build_dub_track(aligned_clips, video_duration, dub_wav)

            # ---- subtitles (T3.2 detect → T3.4 ass → T3.5 single-pass render) ----
            blur_box = None
            ass_path = None
            position = job.options.get("subtitle_position", "bottom")
            if job.options.get("remove_original_subtitles", True):
                await _update(job_id, "detecting_subs", 60)
                from subtitles import default_box, detect_subtitle_box

                box = await detect_subtitle_box(source_mp4, position, work_dir=work)
                if box.get("confidence", 0) < 0.3:
                    # couldn't find a stable box -> blur the default band instead of guessing
                    from media import probe as media_probe

                    info = await media_probe(source_mp4)
                    box = default_box(info["width"], info["height"], position)
                blur_box = box
                import json as _json

                (work / "subtitle_box.json").write_text(_json.dumps(box), encoding="utf-8")
            if job.options.get("burn_subtitles", True):
                from media import probe as media_probe
                from subtitles import write_ass

                info = await media_probe(source_mp4)
                box_for_ass = blur_box or {
                    "x": 0,
                    "y": int(info["height"] * 0.75),
                    "w": info["width"],
                    "h": int(info["height"] * 0.2),
                }
                ass_path = write_ass(
                    result,
                    box_for_ass,
                    work / "newsubs.ass",
                    width=info["width"],
                    height=info["height"],
                    style=job.options.get("subtitle_style"),
                )

            translated = work / "translated.mp4"
            await render_translated_video(
                source_mp4,
                dub_wav,
                ass_path,
                translated,
                blur_box=blur_box,
                original_volume_db=job.options.get("original_audio_volume_db", -20),
                mute_original=job.options.get("mute_original", False),
            )
            # upload to S3 if configured
            # Thumbnail for the projects grid (best-effort; never fail the job).
            thumb_path = work / "thumbnail.jpg"
            if not thumb_path.exists():
                try:
                    from media import extract_thumbnail

                    await extract_thumbnail(translated, thumb_path)
                except Exception as exc:
                    logger.warning("thumbnail extraction failed for %s: %s", job_id, exc)

            s3_key = None
            try:
                # check if S3 configured
                import os as _os

                from storage import S3_BUCKET, _client, upload_file

                if _os.getenv("S3_BUCKET"):
                    key = f"results/{job.user_id or 'anon'}/{job_id}/translated.mp4"
                    await asyncio.to_thread(upload_file, translated, key, "video/mp4")
                    s3_key = key
                    # also upload transcript + thumbnail
                    uploads = [
                        ("transcript.md", "text/markdown; charset=utf-8"),
                        ("transcript.srt", "application/x-subrip; charset=utf-8"),
                        ("segments.json", "application/json"),
                        ("thumbnail.jpg", "image/jpeg"),
                    ]
                    for name, content_type in uploads:
                        p = work / name
                        if p.exists():
                            k = f"results/{job.user_id or 'anon'}/{job_id}/{name}"
                            await asyncio.to_thread(upload_file, p, k, content_type)
            except Exception:
                pass
            artifacts = {
                "video": s3_key or str(translated),
                "transcript": str(work / "transcript.md"),
                "thumbnail": str(thumb_path) if thumb_path.exists() else None,
            }
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
            # record duration + segment count so the API can meter usage (T4.3)
            try:
                from media import probe as _probe

                _info = await _probe(source_mp4)
                _seconds = int(round(_info.get("duration") or 0))
                _nseg = len(result.segments) if hasattr(result, "segments") else 0
                await jobs.update_job(job_id, duration_seconds=_seconds, segments_count=_nseg)
            except Exception:
                pass
            # observability: one log line per job (T5.3)
            try:
                _dur = time.time() - job.created_at
                _segs = (
                    len(result.segments)
                    if "result" in locals() and hasattr(result, "segments")
                    else 0
                )
                logger.info(
                    "job completed",
                    extra={
                        "job_id": job_id,
                        "user_id": job.user_id,
                        "duration": round(_dur, 1),
                        "segments": _segs,
                        "artifacts": artifacts,
                        "request_id": job.request_id,
                    },
                )
            except Exception:
                pass
            return
        # if already done, just mark completed
        await _update(job_id, "done", 100, status="completed")

    except asyncio.CancelledError:
        await jobs.update_job(
            job_id, status="canceled", error={"code": "CANCELED", "message": "canceled"}
        )
        raise
    except Exception as exc:
        code = getattr(exc, "code", "FAILED")
        msg = getattr(exc, "message", str(exc))
        await jobs.update_job(
            job_id, status="failed", error={"code": code, "message": msg}, progress=100
        )
        raise

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from app.services import custom_voices
from app.services.custom_voices import CustomVoiceError, CustomVoiceNotFound

router = APIRouter()


def _owner(request: Request) -> int:
    raw = request.query_params.get("owner")
    if raw is None or not raw.isdigit() or int(raw) <= 0:
        raise HTTPException(status_code=400, detail="owner query parameter is required")
    return int(raw)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or ""


@router.post("/v1/voices/clone", response_model=None)
async def clone_voice(request: Request) -> dict:
    owner = _owner(request)
    data = await request.body()
    try:
        result = custom_voices.clone_voice(
            owner=owner,
            name=request.query_params.get("name", ""),
            language=request.query_params.get("language", "vi"),
            filename=request.query_params.get("filename", "reference.wav"),
            data=data,
            request_id=_request_id(request),
        )
    except CustomVoiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    request.state.engine = "vieneu"
    request.state.language = result.language
    return {
        "voice_id": result.voice_id,
        "name": result.name,
        "language": result.language,
        "engine": result.engine,
        "sample_key": result.sample_key,
        "duration_ms": result.duration_ms,
    }


@router.delete("/v1/voices/clone/{voice_id}", response_model=None)
async def delete_voice(request: Request, voice_id: str) -> Response:
    owner = _owner(request)
    try:
        custom_voices.delete_voice(owner, voice_id)
    except CustomVoiceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=204)


@router.get("/v1/voices/clone/{voice_id}/sample", response_model=None)
async def voice_sample(request: Request, voice_id: str) -> Response:
    owner = _owner(request)
    try:
        data = custom_voices.get_sample(owner, voice_id)
    except CustomVoiceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(content=data, media_type="audio/mpeg")

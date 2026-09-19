# tts-worker

Standalone text-to-speech worker: **VieNeu** for Vietnamese, **edge-tts** for other languages,
**ffmpeg** for merging/transcoding. FastAPI + Uvicorn, deployed as a container behind Caddy.

- Public endpoint: `https://tts-api.aitransclips.com`
- API: `GET /health/live`, `GET /health/ready`, `GET /v1/voices`, `POST /v1/tts`
- Auth: HMAC-signed requests on `/v1/*`; health endpoints are unauthenticated.

## Requirements

- Python 3.12 and [`uv`](https://docs.astral.sh/uv/)
- `ffmpeg` on `PATH`

## Local development

```bash
uv sync                                   # create .venv and install deps
cp .env.example .env                      # then edit secrets
uv run uvicorn app.main:app --reload --port 8004
```

The worker binds `0.0.0.0:8004`. `/docs`, `/redoc`, and `/openapi.json` are always enabled unless
`DISABLE_DOCS=true`. In production they stay reachable but require HTTP Basic auth with
`DOCS_PASSWORD` (any username, `DOCS_PASSWORD` as the password); `DOCS_PASSWORD` is mandatory in
production unless docs are disabled.

## API

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/health/live` | none | Process/event loop is up |
| GET | `/health/ready` | none | Dependencies usable (ffmpeg, output dir, model cache, keys, concurrency) |
| GET | `/v1/voices` | HMAC | Languages + voices catalog |
| POST | `/v1/tts` | HMAC | Generate audio; returns `audio/mpeg` or `audio/wav` |

`POST /v1/tts` body:

```json
{ "text": "Xin chào", "language": "vi", "voice": "Adam", "format": "mp3" }
```

- `language` must be one of the catalog languages (`vi`, `en`, `zh`, `ja`, `ko`, `fr`, `de`, `es`).
- `voice` must belong to the selected language/engine (see `/v1/voices`).
- `text` is limited by `MAX_TEXT_LENGTH`; synchronous requests also respect `SYNC_MAX_TEXT_LENGTH`.
- `format` is `mp3` or `wav`.

## Request signing (HMAC)

`/v1/*` requests must include:

```
X-TTS-Key-Id
X-TTS-Timestamp        # unix seconds
X-TTS-Nonce            # unique per request
X-TTS-Content-SHA256   # hex sha256 of the raw body
X-TTS-Signature        # hex hmac_sha256(secret, canonical)
```

Canonical string (newline-separated):

```
METHOD
PATH_AND_QUERY
UNIX_TIMESTAMP
NONCE
HEX_SHA256_OF_BODY
```

Example signer:

```python
import hashlib, hmac, time, uuid


def sign(secret, key_id, method, path, body=b""):
    ts, nonce = str(int(time.time())), uuid.uuid4().hex
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = "\n".join([method, path, ts, nonce, body_hash])
    return {
        "X-TTS-Key-Id": key_id,
        "X-TTS-Timestamp": ts,
        "X-TTS-Nonce": nonce,
        "X-TTS-Content-SHA256": body_hash,
        "X-TTS-Signature": hmac.new(
            secret.encode(), canonical.encode(), hashlib.sha256
        ).hexdigest(),
    }
```

The worker accepts multiple keys via `HMAC_KEYS_JSON` for dual-key rotation, enforces a timestamp
skew window, and rejects replayed nonces.

## Configuration

Environment variables (see `.env.example` for the full contract):

| Variable | Purpose |
|---|---|
| `APP_ENV` | `development` \| `staging` \| `production` |
| `LOG_LEVEL` | `debug` \| `info` \| `warning` \| `error` |
| `HOST`, `PORT` | bind address (default `0.0.0.0:8004`) |
| `DISABLE_DOCS` | disable `/docs`, `/redoc`, `/openapi.json` |
| `DOCS_PASSWORD` | password (Basic auth) for the docs; required in production when docs are enabled |
| `MAX_TEXT_LENGTH`, `SYNC_MAX_TEXT_LENGTH` | input limits |
| `TTS_CONCURRENCY` | app-level concurrency cap |
| `TTS_FORMAT`, `TTS_OUTPUT_DIR` | default format and output dir |
| `HF_HOME` | model cache directory |
| `FFMPEG_BIN` | ffmpeg binary (defaults to `PATH`) |
| `VIENEU_BACKEND`, `VIENEU_DEFAULT_VOICE`, `VIENEU_CHUNK_CHARS` | VieNeu settings |
| `EDGE_FALLBACK_VOICE`, `EDGE_CHUNK_CHARS` | edge-tts settings |
| `HMAC_KEYS_JSON` | JSON map `{ keyId: secret }` the worker accepts |
| `HMAC_MAX_SKEW_SECONDS`, `HMAC_NONCE_TTL_SECONDS` | replay protection |
| `REQUEST_MAX_BODY_BYTES` | app-level body cap |
| `REQUEST_ID_HEADER`, `ACCESS_LOG_ENABLED` | tracing + access logs |
| `READINESS_WARMUP` | warm the VieNeu model on boot |

## Tests and lint

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## Docker

```bash
docker build -t tts-worker .
docker run --rm -p 127.0.0.1:8004:8004 \
  -e APP_ENV=development \
  -e HMAC_KEYS_JSON='{"local":"change-me"}' \
  tts-worker
```

The image runs as non-root (uid 10001), installs ffmpeg, uses `HF_HOME=/data/hf-cache`, and exposes
`/health/live` as its healthcheck.

## Deployment

See [`DEPLOYMENT.md`](DEPLOYMENT.md) for the production setup, [`deploy/MONITORING.md`](deploy/MONITORING.md)
for operations, and [`DEPLOYMENT_TICKETS.md`](DEPLOYMENT_TICKETS.md) for the implementation history.

## Project layout

```
app/
  main.py                 # FastAPI app, middleware wiring, lifespan
  api/routes/             # health, voices, tts
  core/                   # config, logging, security (HMAC), errors, middleware
  schemas/                # request/response models
  services/               # synthesis, engine_router, voice_catalog, chunking, storage
tests/                    # pytest suite
deploy/                   # docker-compose, Caddyfile, scripts, monitoring runbook
.github/workflows/        # ci.yml, deploy.yml
```

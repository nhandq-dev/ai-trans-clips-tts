# TTS Worker — Deployment Implementation Tickets

Workable implementation tickets for turning the current flat FastAPI worker in this repo
(`AiTransClipsTTS`) into the production `tts-worker` service described in `DEPLOYMENT.md`.

Tickets are ordered by dependency and implementation priority. Each ticket is independently
reviewable and should leave the repo in a working state.

## Confirmed decisions (from planning)

| Topic | Decision |
|---|---|
| Repository | This repo is the dedicated `tts-worker` repo; refactor in place. |
| Scope | Full Phase 1 (refactor + deploy). Async jobs and Redis replay store are Phase 3. |
| API/auth | Clean break: HMAC signing + `/v1/*` + `/health/live|ready`. No legacy `X-Worker-Secret` or old paths. |
| Python/tooling | Upgrade to Python 3.12 + `uv` + `uv.lock`. |
| Tests/CI | CI pipeline with ruff + Docker build; minimal pytest (auth, validation, health). |
| Domain | `tts-api.aitransclips.com` |
| VPS | `103.116.104.223` (Singapore) |
| GitHub / GHCR | `nhandq-dev` → `ghcr.io/nhandq-dev/tts-worker` |
| Access / deploy | Operator keeps root **password** SSH access (no SSH hardening). CI deploys via GitHub Actions SSH as `root` using a dedicated key. |

## Assumptions and technical notes to validate

1. **Caddy ↔ worker networking.** `DEPLOYMENT.md` runs Caddy as a host systemd service but
   proxies to the Docker alias `tts-worker:8004`. A host process cannot resolve a Docker
   network alias. **Decision:** publish the worker only on loopback (`127.0.0.1:8004`) and have
   host Caddy proxy to `127.0.0.1:8004`; `ufw` keeps 8004 closed externally. Running Caddy in
   Docker on the `tts` network is a deferred alternative.
2. **`vieneu==3.3.0` on Python 3.12.** Must be verified in T01 before the restructure proceeds;
   if it fails, fall back to Python 3.10 and re-scope the base image.
3. **Language allowlist source.** `voices.LANGUAGES` exposes 8 languages, while
   `tts_engine.EDGE_DEFAULT_VOICES` supports 15. T06 treats `voices.LANGUAGES` as the canonical
   allowlist (matching `/v1/voices`); confirm if the extra engine languages should be exposed.
4. **`/v1/tts` response shape.** Kept as raw audio bytes (`FileResponse`), matching current
   `POST /tts`. `POST /tts/meta` (JSON metadata) is dropped in the clean break.
5. **Retired artifacts.** `requirements.txt`, `deploy/tts-worker.service`, and the old `.env`
   variable set are replaced by `pyproject.toml` + `uv.lock`, Compose, and the new env contract.

---

## T01 — Tooling and repository baseline

**Goal:** Establish reproducible Python 3.12 tooling and the GitHub remote before any code moves.

### Acceptance Criteria
- [ ] `pyproject.toml` defines project metadata, runtime deps, and a `dev` dependency group.
- [ ] `uv.lock` exists and `uv sync --frozen` succeeds on Python 3.12.
- [ ] `vieneu==3.3.0` imports and instantiates on Python 3.12 (or the fallback decision is recorded).
- [ ] `ruff check .` and `ruff format --check .` pass on the current code.
- [ ] `.gitignore` covers `.venv/`, `__pycache__/`, `tts_output/`, `data/`, `.env`, `*.pyc`.
- [ ] Repo has an initial commit and a remote at `github.com/nhandq-dev/tts-worker`.

### Technical Details
- Runtime deps (from current `requirements.txt`): `fastapi`, `uvicorn[standard]`, `pydantic`,
  `python-multipart`, `vieneu==3.3.0`, `edge-tts`, `soundfile`, `numpy`.
- Dev deps: `ruff`, `pytest`, `httpx` (for TestClient), `pytest-asyncio` if needed.
- Add `[tool.ruff]` config (target `py312`, line length consistent with existing style).
- Verify engine import: `uv run python -c "from vieneu import Vieneu; Vieneu(backend='onnx')"`.
- Remove `requirements.txt` once `pyproject.toml` is authoritative.

### Dependencies
None. Blocks all other tickets.

---

## T02 — Core configuration, logging, and error handling

**Goal:** Centralize settings with fail-fast validation and add structured JSON logging.

### Acceptance Criteria
- [ ] `app/core/config.py` loads all env vars via `pydantic-settings`, validates types, and raises
      a clear startup error when a required value is missing (notably `HMAC_KEYS_JSON` in production).
- [ ] `app/core/logging.py` emits structured JSON to stdout with the fields listed in the plan.
- [ ] `app/core/errors.py` returns clean JSON error bodies for 4xx/5xx without leaking stack traces.
- [ ] `X-Request-Id` is honored when present and generated otherwise, and included in every log line.
- [ ] App settings are injectable/testable (no import-time env reads scattered across modules).

### Technical Details
- Settings groups: app (`APP_ENV`, `LOG_LEVEL`, `HOST`, `PORT`, `DISABLE_DOCS`), TTS
  (`MAX_TEXT_LENGTH`, `SYNC_MAX_TEXT_LENGTH`, `TTS_CONCURRENCY`, `TTS_OUTPUT_DIR`, `HF_HOME`,
  `FFMPEG_BIN`, `VIENEU_BACKEND`, `VIENEU_DEFAULT_VOICE`, `EDGE_FALLBACK_VOICE`), security
  (`HMAC_KEYS_JSON`, `HMAC_MAX_SKEW_SECONDS`, `HMAC_NONCE_TTL_SECONDS`, `REQUEST_MAX_BODY_BYTES`),
  ops (`REQUEST_ID_HEADER`, `ACCESS_LOG_ENABLED`, `READINESS_WARMUP`).
- Log fields per request: `timestamp`, `level`, `request_id`, `method`, `path`, `status_code`,
  `duration_ms`, `engine`, `language`, `text_length`.
- Do not log raw request text, secrets, signature headers, or expected-error stack traces.
- `SYNC_MAX_TEXT_LENGTH` is new; propose default `2000` and make it configurable.
- Replace the scattered `os.getenv` calls currently in `main.py:20-23` and `tts_engine.py:15-43`.

### Dependencies
T01.

---

## T03 — Restructure into the `app/` package (behavior-preserving)

**Goal:** Move the flat modules into the planned package layout without changing runtime behavior.

### Acceptance Criteria
- [ ] `app/main.py` exposes the FastAPI `app`; `uv run uvicorn app.main:app` boots.
- [ ] Engine logic is split into `app/services/` modules; no duplicated logic remains.
- [ ] `voices.py` content lives in `app/services/voice_catalog.py` and is imported by the app.
- [ ] `scripts/generate_samples.py` still runs against the new module paths.
- [ ] Existing endpoints still behave the same (this ticket is a pure move; contract changes are T04).

### Technical Details
- Mapping from current files:
  - `main.py` → `app/main.py` (+ `app/api/routes/` stubs wired in T04).
  - `tts_engine.py` → `app/services/synthesis.py` (VieNeu/edge synth + merge),
    `engine_router.py` (`lang_code`, `is_vietnamese`, `engine_for`),
    `chunking.py` (`_split_text`), `storage.py` (`_ffmpeg`, `_run`, `_transcode`, `_concat`,
    output dir + temp cleanup).
  - `voices.py` → `app/services/voice_catalog.py`.
- Preserve the `_INFER_LOCK` / `_MODEL_LOCK` model-singleton behavior; single process, concurrency
  controlled by the app semaphore.
- Keep `MAX_TEXT_LENGTH`/`TTS_CONCURRENCY` behavior identical in this ticket; T02 handles config.

### Dependencies
T01. Recommended after T02 to avoid editing files twice.

---

## T04 — New public API surface and schemas

**Goal:** Replace the legacy routes with the `/v1/*` + split-health contract.

### Acceptance Criteria
- [ ] `GET /health/live` returns 200 while the process/event loop is up.
- [ ] `GET /health/ready` verifies ffmpeg, output dir writable, HF cache dir accessible, signing
      keys loaded, and semaphore initialized; returns 503 with a reason list when any check fails.
- [ ] `GET /v1/voices` returns the catalog (languages + voices) with no behavior regression.
- [ ] `POST /v1/tts` generates and returns audio bytes (`audio/mpeg` or `audio/wav`) and deletes
      the temp file after the response.
- [ ] Legacy routes `/health`, `/voices`, `/tts`, `/tts/meta` are removed.
- [ ] `DISABLE_DOCS=true` disables `/docs` and `/openapi.json`; enabled only outside production.

### Technical Details
- Files: `app/api/routes/health.py`, `voices.py`, `tts.py`; schemas in `app/schemas/tts.py`,
  `app/schemas/health.py`.
- `/health/live` and `/health/ready` are exempt from HMAC (used by uptime checks and the
  container healthcheck).
- Readiness must not run a full synthesis (per plan); check binaries/dirs/keys only.
- `READINESS_WARMUP` controls whether models warm on boot or first request.
- Request model: `text`, `language` (default `vi`), `voice` (optional), `format` (`mp3`|`wav`).

### Dependencies
T02, T03.

---

## T05 — HMAC request signing and replay protection

**Goal:** Implement the signed-request contract and enforce it before route handlers.

### Acceptance Criteria
- [ ] Requests to `/v1/*` require `X-TTS-Key-Id`, `X-TTS-Timestamp`, `X-TTS-Nonce`,
      `X-TTS-Content-SHA256`, `X-TTS-Signature`; missing/invalid → 401 with a clean body.
- [ ] Canonical string matches the plan: `METHOD\nPATH_AND_QUERY\nTIMESTAMP\nNONCE\nBODY_SHA256`.
- [ ] Signature verified as `hex(hmac_sha256(secret, canonical))` using `hmac.compare_digest`.
- [ ] Requests outside `HMAC_MAX_SKEW_SECONDS` (default 60) are rejected.
- [ ] Replayed nonces within `HMAC_NONCE_TTL_SECONDS` (default 300) are rejected.
- [ ] Multiple accepted keys are supported via `HMAC_KEYS_JSON` for dual-key rotation.
- [ ] `/health/live` and `/health/ready` remain unauthenticated.

### Technical Details
- Files: `app/core/security.py` (verify + canonicalization), `app/core/middleware.py` (ASGI
  middleware wired in `app/main.py`).
- Body handling: read the raw body once, verify the SHA-256, then replay it to the handler
  (custom `receive`/`request._body`) so downstream parsing still works.
- Nonce store: in-memory TTL cache guarded by a lock; process restart clears it (accepted trade-off).
- Constant-time compare only; never log secrets or signature headers.

### Dependencies
T02, T03, T04.

---

## T06 — Request limits, strict validation, and CORS/docs hardening

**Goal:** Enforce server-side limits and remove permissive defaults.

### Acceptance Criteria
- [ ] `REQUEST_MAX_BODY_BYTES` enforced at the app layer; oversized requests → 413.
- [ ] `language` validated against the canonical allowlist; unknown → 400.
- [ ] `format` restricted to `mp3`/`wav`; unsupported → 400 before synthesis.
- [ ] `voice` validated to belong to the resolved engine/language; invalid → 400.
- [ ] `text` length enforced against `MAX_TEXT_LENGTH`; over `SYNC_MAX_TEXT_LENGTH` → clean 400.
- [ ] `CORSMiddleware` removed entirely (the browser never calls the worker).
- [ ] Malformed JSON and unsupported content types return clean 4xx errors.

### Technical Details
- Files: `app/schemas/tts.py` (validators), `app/services/voice_catalog.py` (allowlist lookups),
  `app/api/routes/tts.py` (pre-synthesis checks).
- Current `main.py` accepts any `language` and any `voice` string and relies on engine fallbacks
  (`tts_engine.py:73-82`); replace with strict validation.
- Align the language allowlist with `voices.LANGUAGES` (see Assumption 3).
- Keep error messages caller-safe (no internals).

### Dependencies
T02, T04.

---

## T07 — Production Dockerfile

**Goal:** Build a small, non-root, reproducible image with ffmpeg and a healthcheck.

### Acceptance Criteria
- [ ] Base image `python:3.12-slim`; dependencies installed from `uv.lock` with `uv sync --frozen --no-dev`.
- [ ] `ffmpeg` and `curl` present; no build toolchain left in the final layer.
- [ ] Container runs as non-root user (uid 10001) with `/app` and `/data` owned by it.
- [ ] `HF_HOME=/data/hf-cache`; `/data/hf-cache` and `/data/output` created and writable.
- [ ] `HEALTHCHECK` calls `/health/live`; `EXPOSE 8004`; CMD runs `uvicorn app.main:app`.
- [ ] `docker build` succeeds and the image runs locally with a signed smoke request.

### Technical Details
- Follow the reference Dockerfile in `DEPLOYMENT.md`, adjusted to `app.main:app` and `uv.lock`.
- Only copy `app/` (and any needed runtime files) into the image; never bake `.env` or secrets.
- Remove the old root-based `Dockerfile` behavior (current `Dockerfile` uses 3.10 + pip + root).

### Dependencies
T01, T03, T04.

---

## T08 — Compose stack, Caddyfile, and deploy assets

**Goal:** Provide the on-VPS runtime assets for the worker and reverse proxy.

### Acceptance Criteria
- [ ] `deploy/docker-compose.yml` runs the worker with `restart: unless-stopped`, `env_file: .env`,
      loopback publish `127.0.0.1:8004:8004`, log rotation, and a `/health/ready` healthcheck.
- [ ] Persistent volumes map `./data/hf-cache` and `./data/output`.
- [ ] `deploy/Caddyfile` serves `tts-api.aitransclips.com`, redirects HTTP→HTTPS, enforces a 2MB
      body limit, proxies to `127.0.0.1:8004`, and writes JSON access logs.
- [ ] `deploy/.env.example` documents the full new env contract (app, TTS, security, ops) with no secrets.
- [ ] `deploy/scripts/deploy.sh`, `rollback.sh`, and `healthcheck.sh` exist and are executable.
- [ ] Obsolete `deploy/tts-worker.service` (host-venv systemd unit) is removed.

### Technical Details
- Caddy runs as a host systemd service; it reaches the worker via loopback (see Assumption 1).
- `deploy.sh`: set `IMAGE_TAG`, `docker compose pull`, `up -d`, poll readiness, return non-zero on failure.
- `rollback.sh`: reset `IMAGE_TAG` to the recorded previous good SHA and restart.
- `healthcheck.sh`: curl `/health/live` and `/health/ready` on the internal port and the public URL.
- Caddy `response_header_timeout` (90s) must match the worker's max synchronous processing time.

### Dependencies
T04, T05, T07.

---

## T09 — Provision the Singapore VPS

**Goal:** Prepare `103.116.104.223` (Ubuntu 22.04.5 LTS) to host the stack.

### Acceptance Criteria
- [ ] `docker-ce`, `docker-compose-plugin`, `caddy`, `ufw`, `fail2ban`, `unattended-upgrades`,
      `curl`, `jq`, `htop` installed; time sync healthy (`systemd-timesyncd` or `chrony`).
- [ ] Root SSH and password access left intact for the operator (no SSH hardening).
- [ ] Optional CI deploy public key installed into root's `authorized_keys` for GitHub Actions.
- [ ] `ufw` allows only `22`, `80`, `443`; provider firewall matches; worker port not public.
- [ ] Docker log rotation configured; `/opt/tts-worker/.env` owned by `root` with `600`.
- [ ] On-disk layout created: `/opt/tts-worker/{releases,scripts,data/hf-cache,data/output}`.

### Technical Details
- Keep Caddy as a host systemd service; the worker runs via `docker compose`.
- The operator keeps password access; CI authenticates with a dedicated SSH key (T12).
- `data/hf-cache` persists VieNeu/HF assets so cold starts do not redownload.
- Set 1–2 GB swap as a safety net only; no horizontal scaling on day one.
- HMAC timestamp validation depends on accurate clock; confirm drift after install.
- Implemented by `deploy/scripts/provision.sh`.

### Dependencies
T08 (assets must exist to copy).

---

## T10 — DNS and TLS

**Goal:** Point the domain at the VPS and obtain a valid certificate.

### Acceptance Criteria
- [ ] PA Vietnam `A` record `tts-api` → `103.116.104.223` exists (TTL 300 during rollout).
- [ ] DNS resolves to the VPS from a public network.
- [ ] Ports 80/443 reachable; Caddy obtains a Let's Encrypt certificate for
      `tts-api.aitransclips.com`.
- [ ] `https://tts-api.aitransclips.com/health/live` returns 200 from a public network.
- [ ] HTTP requests redirect to HTTPS; no self-signed certs; TLS verification works.

### Technical Details
- ACME HTTP-01/TLS-ALPN only need the hostname to resolve and 80/443 open; PA Vietnam DNS host is
  irrelevant to issuance.
- Caddy needs a persistent writable data dir for cert storage (default host path).
- Raise TTL to 3600 once stable.

### Dependencies
T09.

---

## T11 — CI workflow

**Goal:** Automated checks on every PR/push.

### Acceptance Criteria
- [ ] `.github/workflows/ci.yml` runs on PR and push.
- [ ] Steps: `uv sync --frozen`, `ruff check`, `ruff format --check`, `pytest`, `docker build`.
- [ ] Minimal pytest covers HMAC verify/replay and health/validation paths.
- [ ] Pipeline is green on `main` before enabling CD.

### Technical Details
- Use `astral-sh/setup-uv` with Python 3.12 and cache.
- Tests: `tests/test_signature_auth.py`, `tests/test_health.py`, `tests/test_tts_validation.py`.
- "Minimal tests" scope: signature accept/reject, skew/nonce rejection, `/health/live|ready`,
  format/language/voice validation. Full synthesis is not exercised in CI.

### Dependencies
T05, T06, T07.

---

## T12 — CD workflow and rollback

**Goal:** Deploy immutable images to the VPS with a health-check gate.

### Acceptance Criteria
- [ ] `.github/workflows/deploy.yml` triggers on push to `main`.
- [ ] Builds `ghcr.io/nhandq-dev/tts-worker:<commit-sha>` (plus `latest`), pushes to GHCR.
- [ ] SSHs to the VPS as `root` with a dedicated CI key, sets `IMAGE_TAG`, and runs
      `deploy/scripts/deploy.sh` (`docker compose pull` + `up -d`).
- [ ] Waits for internal `/health/ready` and public `/health/live` to pass.
- [ ] On failure, automatically redeploys the previous recorded good tag via `rollback.sh`.
- [ ] Required secrets configured: `VPS_HOST=103.116.104.223`, `VPS_USER=root`,
      `VPS_SSH_KEY` (private key), `GHCR_TOKEN`.

### Technical Details
- The operator generates a CI key pair; the public half goes into root's `authorized_keys`
  (see `CI_DEPLOY_PUBKEY` in `provision.sh`). The private half is stored only as `VPS_SSH_KEY`.
- Record the previous good tag on the VPS (e.g. `/opt/tts-worker/releases/previous_tag`) for rollback.
- Deploy only from `main`; single-container replacement with a short restart window is acceptable.
- `HMAC_KEYS_JSON` and other runtime secrets live only in the VPS `.env`, never in the image or CI logs.

### Dependencies
T08, T09, T10, T11.

---

## T13 — Post-deploy smoke tests, monitoring, and cleanup

**Goal:** Verify the real path after each deploy and keep the host healthy.

### Acceptance Criteria
- [ ] A smoke script runs signed `GET /v1/voices` and a signed short `POST /v1/tts`.
- [ ] Deploy workflow runs the smoke test and fails the deploy if it fails.
- [ ] External uptime check configured against `/health/ready`.
- [ ] CPU/RAM/disk alerts and a cache/output disk-growth alert are configured.
- [ ] Temp audio cleanup and log rotation are verified in place.

### Technical Details
- Smoke signing can be a small `scripts/sign_request.py` helper reused by the deploy script.
- Validates end to end: HTTPS routing, HMAC, ffmpeg, engine init, real audio generation.
- Cleanup: temp files removed after responses (already via `BackgroundTask`); workdirs cleaned in
  `generate_tts`'s `finally`; add retention for `data/output` if outputs are ever retained.

### Dependencies
T12.

---

## T14 — Documentation sync

**Goal:** Make repo docs match the shipped system.

### Acceptance Criteria
- [ ] `README.md` covers setup, env vars, endpoints, signing contract, and local run.
- [ ] `CHANGELOG.md` records the initial production release.
- [ ] `DEPLOYMENT.md` updated with the real domain, VPS IP, loopback-proxy decision, and the new
      API/auth contract; stale paths (systemd unit, old env names) removed.

### Technical Details
- Document the canonical signing string and required headers so the caller can integrate.
- Note the Phase 3 items (async jobs, Redis nonce store, blue/green) as explicitly deferred.

### Dependencies
T01–T13.

---

## Dependency summary (critical path)

```text
T01 → T02 → T03 → T04 → T05 → T06
                    │
                    ├→ T07 → T08 → T09 → T10
                    │        │
                    └────────┴→ T11 → T12 → T13 → T14
```

T02/T03 should land before T04. T07 can start once T03/T04 are in place. T09 requires T08 assets.
T11 can run in parallel with T09/T10 once T05/T06/T07 are merged.

# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added

- `DOCS_PASSWORD`: HTTP Basic auth for `/docs`, `/redoc`, and `/openapi.json`.

### Changed

- Docs are no longer auto-disabled in production. They stay reachable but require
  `DOCS_PASSWORD`; startup fails in production when docs are enabled without a password.
  Set `DISABLE_DOCS=true` to remove them entirely.

## [1.0.0] - 2026-09-18

Initial production release of the standalone `tts-worker`.

### Added

- `app/` package: FastAPI app, route modules, core concerns (config, JSON logging, HMAC security,
  error handlers, request-context middleware), schemas, and synthesis services.
- Public API: `GET /health/live`, `GET /health/ready`, `GET /v1/voices`, `POST /v1/tts`.
- HMAC request signing with timestamp skew and nonce replay protection, plus dual-key rotation.
- Strict request validation: language allowlist, format, text length (max + sync), and voice
  membership; app-level body-size limit.
- Structured JSON logging with `X-Request-Id` correlation.
- Reproducible Python 3.12 tooling via `uv` (`pyproject.toml`, `uv.lock`).
- Production Docker image: non-root (uid 10001), ffmpeg, `HF_HOME=/data/hf-cache`, healthcheck.
- Deploy assets: `docker-compose.yml`, `Caddyfile`, `deploy/scripts` (provision, deploy, rollback,
  healthcheck, smoke), and `deploy/.env.example`.
- CI (`.github/workflows/ci.yml`): ruff, pytest, docker build.
- CD (`.github/workflows/deploy.yml`): build + push to GHCR, SSH deploy as root, signed smoke test,
  automatic rollback.
- Minimal pytest suite covering auth, health, and validation.
- Documentation: `README.md`, `DEPLOYMENT.md`, `deploy/MONITORING.md`.

### Changed

- Replaced the static shared-secret auth and flat modules with the HMAC-based `/v1` API and the
  `app/` package layout.
- Moved DNS authority for `aitransclips.com` fully to Vercel and removed the PA Vietnam nameservers.

### Removed

- Legacy routes `/health`, `/voices`, `/tts`, `/tts/meta`.
- Static `X-Worker-Secret` auth and the `TTS_WORKER_SECRET` env var.
- Permissive CORS.
- The host-venv `tts-worker.service` systemd unit and `requirements.txt`.

# TTS Worker — Deployment Guide

Production deployment guide for the standalone `tts-worker` service: the worker itself, its
repository layout, and its deployment on a Singapore VPS behind a reverse proxy at
`https://tts-api.aitransclips.com`.

Scope: **worker service + VPS only.** Anything about the main API, frontend, or other services is
out of scope except where it defines the request contract the worker must verify.

## As-built production configuration

| Item | Value |
|---|---|
| Public endpoint | `https://tts-api.aitransclips.com` |
| VPS | `103.116.104.223` (Singapore, Ubuntu 22.04.5 LTS) |
| Reverse proxy | Caddy on the host (systemd), automatic Let's Encrypt TLS |
| Worker runtime | Docker Compose; image `ghcr.io/nhandq-dev/tts-worker` |
| Networking | Worker published on **loopback only** (`127.0.0.1:8004`); Caddy proxies to it |
| DNS | **Vercel DNS** is authoritative (`ns1,ns2.vercel-dns.com`); PA Vietnam NS removed |
| Auth | HMAC-signed requests on `/v1/*`; `/health/*` unauthenticated |
| CI/CD | GitHub Actions → GHCR → SSH deploy (as `root` with a dedicated key) |
| Repo layout | `app/` package, `deploy/`, `tests/`, `.github/workflows/` |

Implementation history and decisions are tracked in `DEPLOYMENT_TICKETS.md`; day-one operations are
in `deploy/MONITORING.md`.


## Architecture

```text
Authorized caller (main API)
  -> HTTPS request to https://tts-api.aitransclips.com
  -> HMAC-signed request headers
Caddy on Singapore VPS (:443)
  -> reverse proxy to 127.0.0.1:8004 (loopback; worker port is not public)
FastAPI tts-worker container
  -> VieNeu (Vietnamese) / edge-tts (other languages) / ffmpeg
```

Key decisions:

1. `tts-worker` lives in its own repository with its own CI/CD pipeline.
2. It runs on a Singapore VPS to keep latency low for the primary caller.
3. Only `443` and `80` are exposed publicly (TLS + certificate issuance).
4. The worker is published only on loopback (`127.0.0.1:8004`), never internet-facing.
5. It is addressed by `https://tts-api.aitransclips.com`, not a raw IP, so TLS and cert management stay standard.
6. Requests are authenticated with HMAC request signing, not source IP allowlisting.
7. Start with synchronous generation; add async jobs only if real traffic requires it.

Why this is the right balance:

- More secure than a static secret header alone.
- Simpler than mTLS or a private overlay network.
- More reliable than IP allowlisting, since callers may have dynamic egress IPs.
- Low-latency, because the VPS sits in the same region as the caller.
- Easy to operate on a single VPS without over-engineering.

---

## 1. Repository structure

The repository is a small, self-contained Python service with its deployment assets included.

```text
tts-worker/  (repo: ai-trans-clips-tts)
  app/
    main.py
    api/
      routes/
        health.py
        voices.py
        tts.py
    core/
      config.py
      logging.py
      security.py
      errors.py
      middleware.py
    services/
      synthesis.py
      engine_router.py
      voice_catalog.py
      chunking.py
      storage.py
    schemas/
      tts.py
      health.py
  tests/
    conftest.py
    test_health.py
    test_signature_auth.py
    test_tts_validation.py
  deploy/
    docker-compose.yml
    Caddyfile
    .env.example
    MONITORING.md
    scripts/
      provision.sh
      deploy.sh
      rollback.sh
      healthcheck.sh
      smoke.py
  .github/
    workflows/
      ci.yml
      deploy.yml
  Dockerfile
  pyproject.toml
  uv.lock
  README.md
  DEPLOYMENT.md
  DEPLOYMENT_TICKETS.md
  CHANGELOG.md
```

Why this structure:

- `app/` separates HTTP concerns from synthesis logic.
- `core/security.py` isolates request-signing verification and replay protection.
- `deploy/` keeps VPS-specific operational files with the repo instead of in separate notes.
- `tests/` focuses on what breaks production: auth, validation, and health checks.
- `pyproject.toml` + `uv.lock` gives reproducible Python builds.

### API surface

Keep the public API small:

1. `GET /health/live`
2. `GET /health/ready`
3. `GET /v1/voices`
4. `POST /v1/tts`

Optional later, only if runtime grows:

1. `POST /v1/jobs`
2. `GET /v1/jobs/:id`
3. `GET /v1/jobs/:id/audio`

Swagger/OpenAPI may be exposed in production, but only behind HTTP Basic auth: set
`DOCS_PASSWORD` and the docs (`/docs`, `/redoc`, `/openapi.json`) require it. `DOCS_PASSWORD` is
mandatory in production unless `DISABLE_DOCS=true` disables the docs entirely.

### Configuration and environment variables

Application:

| Variable | Purpose |
|---|---|
| `APP_ENV` | `development`, `staging`, `production` |
| `LOG_LEVEL` | `info`, `warning`, `error`, `debug` |
| `HOST` | Internal bind host, `0.0.0.0` inside the container |
| `PORT` | Internal app port, `8004` |
| `DISABLE_DOCS` | Disable `/docs`, `/redoc`, and `/openapi.json` entirely |
| `DOCS_PASSWORD` | Basic-auth password for the docs; required in production when docs are enabled |

TTS runtime:

| Variable | Purpose |
|---|---|
| `MAX_TEXT_LENGTH` | Absolute input limit |
| `SYNC_MAX_TEXT_LENGTH` | Max size allowed for synchronous generation |
| `TTS_CONCURRENCY` | App-level concurrency cap |
| `TTS_OUTPUT_DIR` | Temporary or retained output path |
| `HF_HOME` | Hugging Face cache directory |
| `FFMPEG_BIN` | ffmpeg binary path |
| `VIENEU_BACKEND` | ONNX backend selection |
| `VIENEU_DEFAULT_VOICE` | Default Vietnamese voice |
| `EDGE_FALLBACK_VOICE` | Fallback non-Vietnamese voice |

Security:

| Variable | Purpose |
|---|---|
| `HMAC_KEYS_JSON` | JSON map of `{ keyId: secret }` the worker accepts |
| `HMAC_MAX_SKEW_SECONDS` | Allowed request timestamp skew, e.g. `60` |
| `HMAC_NONCE_TTL_SECONDS` | Nonce replay window, e.g. `300` |
| `REQUEST_MAX_BODY_BYTES` | Request size cap at app level |

Operations:

| Variable | Purpose |
|---|---|
| `REQUEST_ID_HEADER` | Header used for trace correlation, e.g. `X-Request-Id` |
| `ACCESS_LOG_ENABLED` | Toggle request access logs |
| `READINESS_WARMUP` | Warm models on boot or on first request |

Guidelines:

1. Keep secrets out of the repo. `.env.example` documents shape only.
2. Store production secrets in GitHub Actions secrets and on the VPS in a root-owned env file.
3. Treat `HMAC_KEYS_JSON` as sensitive and rotate it like any credential.
4. Validate env on startup and fail fast if anything required is missing.

### Dependency and runtime setup

1. Python `3.12` slim image.
2. `uv` for dependency lock and install speed.
3. FastAPI + Uvicorn.
4. Run as a non-root container user.
5. Install `ffmpeg` in the image.
6. Persist model cache on a mounted volume so cold starts do not redownload assets.

### Logging

Production logging is structured JSON to stdout.

Log these fields on every request:

1. `timestamp`
2. `level`
3. `request_id`
4. `method`
5. `path`
6. `status_code`
7. `duration_ms`
8. `engine`
9. `language`
10. `text_length`

Do not log:

1. Raw request text
2. Full secrets or signature headers
3. Full stack traces for expected client errors

Recommendation:

1. App logs in JSON to stdout.
2. Caddy access logs in JSON.
3. Docker log rotation enabled.
4. Honor the incoming `X-Request-Id` for end-to-end correlation.

### Health checks

Use two endpoints:

1. `GET /health/live` — process is up and the event loop is running.
2. `GET /health/ready` — dependencies are usable.

`/health/ready` should verify:

1. ffmpeg is available
2. configured output/temp directories are writable
3. model cache directory is accessible
4. required signing keys are loaded
5. service concurrency semaphore can initialize

Do not make readiness depend on a full synthesis run. That makes health checks slow and noisy.

### Security best practices inside the repo

1. Private app port only. Never expose the worker container port publicly.
2. HMAC verification middleware before route handlers.
3. Timestamp and nonce replay protection.
4. Request body size limits at both proxy and app layers.
5. Disable permissive CORS. The browser never calls the worker directly.
6. Validate `language`, `voice`, `format`, and max text length strictly.
7. Return clean 4xx/5xx errors without leaking stack internals.
8. Run the container as a non-root user.
9. Keep the writable filesystem surface small: temp dir, output dir, cache dir only.
10. Pin dependencies with a lock file and scan them in CI.

---

## 2. VPS setup on Ubuntu 22.04.5 LTS

Recommended production setup:

1. Host OS: `Ubuntu 22.04.5 LTS`
2. Reverse proxy on host: `Caddy`
3. Worker runtime: Docker Compose
4. App image: versioned container image from GitHub Container Registry
5. TLS termination: Caddy with automatic Let's Encrypt certificates

This is simpler and more operationally stable than managing Python directly on the host.

### Base server sizing

Suggested starting point:

1. 2 to 4 vCPU
2. 4 to 8 GB RAM
3. SSD-backed storage
4. 20 to 40 GB disk minimum

Why:

- VieNeu inference and ffmpeg are CPU-bound.
- Hugging Face cache and temp audio files need disk headroom.
- A too-small VPS spends more time swapping than synthesizing.

If budget is tight, start with 2 vCPU / 4 GB RAM and keep concurrency low.

### Required packages and services

Install and configure:

1. `docker-ce`
2. `docker-compose-plugin`
3. `caddy`
4. `ufw`
5. `fail2ban`
6. `unattended-upgrades`
7. `curl`
8. `jq`
9. `htop`

Keep time synced. HMAC timestamp validation depends on correct clock drift.

Recommended:

1. use `systemd-timesyncd` if already healthy
2. otherwise install `chrony`

### Host hardening

Applied by `deploy/scripts/provision.sh`:

1. Root **password** SSH access is kept for the operator (no SSH hardening).
2. A dedicated CI key is added to root's `authorized_keys` for GitHub Actions deploys.
3. Enable `ufw`.
4. Open only `22`, `80`, and `443` publicly.
5. Do not open the worker container port publicly (loopback publish only).
6. Enable unattended security updates.
7. Set Docker log rotation limits.
8. Restrict `/opt/tts-worker/.env` to `root:600`.

Recommended `ufw` exposure:

1. `22/tcp`
2. `80/tcp`
3. `443/tcp`

Nothing else should be internet-accessible.

### Process and container management

Recommended split:

1. Host-managed `caddy` service via the systemd package.
2. `docker compose` stack for the worker container.

Why this split:

- Caddy cert management stays stable across app deployments.
- App deploys are isolated to the worker container.
- Simpler rollback than mixing host Python and systemd app services.

Recommended on-disk layout:

```text
/opt/tts-worker/
  docker-compose.yml
  .env
  releases/
  scripts/
  data/
    hf-cache/
    output/
```

Recommended Docker volumes:

1. persistent cache volume for Hugging Face/VieNeu assets
2. persistent output/temp volume only if generated audio is retained locally

If audio is streamed and deleted immediately, output retention can stay minimal.

### Resource optimization

Start simple:

1. one app container
2. one Uvicorn process
3. app-level semaphore concurrency of `1` or `2`

Do not scale horizontally on day one unless production traffic justifies it.

Reason:

- CPU TTS workloads degrade when too many concurrent jobs compete.
- One process with explicit concurrency control is more predictable than multiple workers fighting for CPU and cache.

Additional tuning:

1. mount model cache on persistent SSD
2. keep temp files on fast local disk
3. add 1 to 2 GB swap only as a safety net, not as normal operating capacity
4. set compose memory limits if the VPS also runs other workloads

### HTTPS and reverse proxy

Recommended proxy: `Caddy`.

Why Caddy:

1. automatic HTTPS and renewal
2. simpler config than nginx for a single-service VPS
3. reliable reverse proxy behavior

Caddy responsibilities:

1. terminate TLS for `tts-api.aitransclips.com`
2. redirect HTTP to HTTPS
3. reverse proxy to the private worker endpoint
4. enforce request body limits
5. write structured access logs
6. set sane upstream timeouts

Keep the worker private by routing only to an internal Docker network alias like `tts-worker:8004`.

### Monitoring and alerting

Keep monitoring practical:

1. external uptime check against `https://tts-api.aitransclips.com/health/ready`
2. VPS provider CPU/RAM/disk alerts if available
3. log aggregation later if needed

Day-one observability:

1. Caddy access logs
2. worker JSON logs
3. container health status
4. disk usage alert for cache/output volume
5. SSL expiry alert, though Caddy usually handles renewals automatically

Optional later:

1. Sentry for Python exceptions
2. Prometheus/Grafana
3. Better Stack, Datadog, or Grafana Cloud

Do not start with a full observability stack unless the platform already uses one.

---

## 3. DNS setup (Vercel)

The zone `aitransclips.com` is authoritative at **Vercel DNS** (`ns1,ns2.vercel-dns.com`). The
earlier PA Vietnam nameservers were removed from the registrar, so all records live in Vercel.

### Required records

| Type | Name | Value | TTL |
|---|---|---|---|
| `A` | `tts-api` | `103.116.104.223` | 300 during rollout, then 3600 |

Email and mail-authentication records were migrated to Vercel before removing PA Vietnam: `@` MX
(`mail92227.maychuemail.net` / `.com`), `@` TXT SPF, `mx` and `mail` CNAMEs, `send` MX/TXT (Amazon
SES/Resend), `resend._domainkey` TXT, and a single `_dmarc` TXT.

### SSL and certificate considerations

1. point the `A` record at the VPS
2. open ports `80` and `443`
3. Caddy obtains and renews certificates automatically (HTTP-01 or TLS-ALPN-01)
4. keep only `ns1,ns2.vercel-dns.com` at the registrar to avoid split-brain resolution

Operational notes:

1. DNS must propagate before the first certificate issuance succeeds. If Caddy retried while DNS
   still pointed elsewhere, restart it (`systemctl restart caddy`) to trigger an immediate retry.
2. Caddy needs a persistent writable data directory for certificate storage.
3. If port `80` is blocked, issuance requires a DNS-based challenge flow, which adds complexity and
   should be avoided unless necessary.

### DNS checklist

1. Create/confirm the `A` record `tts-api` → `103.116.104.223`.
2. Confirm it resolves to the VPS from outside (`dig +short A tts-api.aitransclips.com @8.8.8.8`).
3. Open `80` and `443` on the VPS and provider firewalls.
4. Verify certificate issuance and `https://tts-api.aitransclips.com/health/live`.
5. Remove the PA Vietnam nameservers, leaving only Vercel.

---

## 4. Worker security and authentication

Recommended approach: `HTTPS + HMAC-signed requests + timestamp + nonce replay protection`.

### Why HMAC

| Approach | Security | Complexity | Operational fit | Recommendation |
|---|---|---|---|---|
| Static bearer secret header | Medium | Low | Simple but replayable | Not preferred |
| HMAC signed request | High | Low to medium | Excellent for a single trusted caller | Recommended |
| Short-lived JWT signed by caller | High | Medium | Good, but more claim management | Acceptable alternative |
| mTLS | Very high | High | Strong, but heavy for a single VPS | Usually too much |

Why HMAC wins here:

1. no extra identity provider or certificate authority setup
2. payload integrity is protected
3. replay protection is straightforward
4. easy to rotate keys without downtime

### Request signing scheme (contract the worker verifies)

Headers the worker expects:

1. `X-TTS-Key-Id`
2. `X-TTS-Timestamp`
3. `X-TTS-Nonce`
4. `X-TTS-Content-SHA256`
5. `X-TTS-Signature`
6. `X-Request-Id`

Canonical string to sign:

```text
<HTTP_METHOD>
<PATH_AND_QUERY>
<UNIX_TIMESTAMP>
<NONCE>
<HEX_SHA256_OF_REQUEST_BODY>
```

Signature:

```text
hex(hmac_sha256(secret, canonical_string))
```

Worker verification steps:

1. Load key by `X-TTS-Key-Id`.
2. Reject if timestamp is outside the allowed skew.
3. Reject if nonce has already been seen within the nonce TTL window.
4. Recompute body hash.
5. Recompute HMAC and compare in constant time.
6. Only then pass to the route handler.

### Replay protection

For a single worker instance, keep replay protection simple.

Day-one design:

1. in-memory TTL cache of recent nonces
2. timestamp skew limit of `60` seconds
3. nonce TTL of `300` seconds

Trade-off:

- A process restart clears the nonce cache, so replay resistance is not perfect across restarts.

Why this is acceptable initially:

1. requests are already protected by TLS
2. the replay window is small
3. the worker is a single private service, not a public multi-tenant API

Upgrade path if needed later:

1. move nonce tracking to Redis
2. use shared replay protection across multiple worker instances

### Credential rotation

Use dual-key rotation, not flag-day rotation.

1. The worker accepts multiple keys via `HMAC_KEYS_JSON`.
2. The caller signs with one active key ID.
3. Rotation process:
   1. add the new key to the worker's accepted keys
   2. deploy the worker
   3. switch the caller to sign with the new key ID and secret
   4. verify traffic
   5. remove the old key from the worker after a safe window

This avoids downtime and keeps rollback simple.

### TLS requirements

HMAC does not replace TLS. Use both.

TLS provides:

1. confidentiality of text and audio
2. protection against passive network inspection
3. server authenticity via certificate validation

Requirements:

1. the public endpoint is `https://tts-api.aitransclips.com`
2. do not disable TLS verification on the caller
3. do not use self-signed certs in production
4. do not use `http://` over the public internet

### Request limits and timeout expectations

The worker defines and documents its own server-side limits so callers can align:

1. max request body size enforced at proxy and app layers
2. max synchronous text length enforced before synthesis
3. a server-side maximum processing time per request, after which the worker returns a clean timeout error
4. callers are expected to set explicit connect and total timeouts (recommended: connect `2s`, headers `5s`, total `45s` to `90s`)

Retry contract:

1. `GET /v1/voices` is safe to retry on `502`, `503`, `504`, and connect failures.
2. `POST /v1/tts` is not automatically retryable unless the caller sends an idempotency key the worker understands.

### Request validation

The worker validates everything it receives, independently of the caller:

1. language code allowed
2. text length within limits
3. format allowed
4. voice belongs to the selected engine/language
5. reject malformed JSON and oversized bodies
6. reject unsupported formats immediately

Never trust the caller as a replacement for worker-side validation.

### Sync vs async recommendation

Recommended starting point:

1. synchronous `POST /v1/tts` for short and medium requests
2. enforce `SYNC_MAX_TEXT_LENGTH`
3. return a clean error when the request exceeds the synchronous limit

Why not force async on day one:

- more endpoints
- more state management
- more storage and cleanup logic

When to add async jobs:

1. if typical synthesis time is regularly above 20 to 30 seconds
2. if timeout pressure appears in logs
3. if callers need background generation and later retrieval

---

## 5. Deployment and operations

Recommended path: GitHub Actions -> build image -> push to GHCR -> SSH deploy on VPS -> `docker compose` update -> health check -> rollback on failure.

### CI pipeline

Run on every PR and push:

1. dependency install from lock file
2. formatter check
3. lint
4. unit tests
5. build container image
6. vulnerability scan on the image or dependencies

Suggested checks:

1. `ruff check`
2. `ruff format --check`
3. `pytest`
4. Docker build

### CD pipeline

Release model:

1. build Docker image tagged with the commit SHA
2. optionally add semantic release tags later
3. push to GitHub Container Registry
4. deploy only from `main`

Deploy job responsibilities:

1. SSH to the VPS as `root` with the dedicated CI key
2. sync `deploy/scripts` and run `deploy/scripts/deploy.sh <sha>` (retires the legacy service,
   sets `IMAGE_TAG`, `docker compose pull` + `up -d`)
3. wait for internal `/health/ready` to pass
4. run the signed smoke test and verify the public `/health/live` endpoint
5. if any step fails, run `deploy/scripts/rollback.sh` to restore the previous tag

### Minimal-downtime deployment strategy

Day-one approach:

1. single container replacement with a health-check gate

Expected result:

- a short restart window, usually a few seconds

Why this is acceptable initially:

1. simple to reason about
2. simple to rollback
3. avoids blue/green complexity on a single VPS

If near-zero downtime is needed later:

1. run two worker containers
2. point Caddy to an upstream pool
3. drain the old instance after the new instance passes readiness

Do not start there unless uptime pressure actually requires it.

### Rollback plan

Keep rollback boring and fast:

1. every release image gets an immutable commit SHA tag
2. keep the previous known-good tag recorded on the VPS
3. `rollback.sh` resets the compose image tag to the previous SHA and restarts the container

Rollback triggers:

1. readiness fails after deploy
2. public health endpoint fails
3. synthesis smoke test fails
4. logs show systemic auth or inference failure immediately after rollout

### Post-deploy smoke tests

After each deploy, run:

1. `GET /health/live`
2. `GET /health/ready`
3. signed `GET /v1/voices`
4. signed short `POST /v1/tts` using a tiny test phrase

This validates:

1. HTTPS routing
2. request signing
3. ffmpeg availability
4. engine initialization
5. the real audio generation path

### Production monitoring and maintenance

Day-one checklist:

1. daily uptime checks
2. CPU/RAM/disk alerts
3. monitor disk growth of cache and outputs
4. monthly host OS package updates
5. dependency updates on a scheduled cadence
6. review Caddy and app logs for repeated upstream or auth failures

Cleanup tasks:

1. delete expired temp audio files automatically
2. keep cache size under review
3. rotate logs

### Secrets management

Store secrets in two places only:

1. GitHub Actions secrets for CI/CD
2. root-owned `.env` on the VPS for runtime

Do not:

1. hardcode secrets in compose files
2. bake secrets into Docker images
3. store production secrets in repo variables visible to contributors

---

## 6. Reference configuration

These reflect the files in the repository (`Dockerfile`, `deploy/docker-compose.yml`, `deploy/Caddyfile`).

### Dockerfile

```dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/data/hf-cache \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app

RUN useradd --create-home --uid 10001 worker \
    && mkdir -p /data/hf-cache /data/output \
    && chown -R worker:worker /data /app
USER worker

EXPOSE 8004

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8004/health/live || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8004"]
```

### docker-compose.yml (`deploy/docker-compose.yml`)

```yaml
services:
  tts-worker:
    image: ghcr.io/nhandq-dev/tts-worker:${IMAGE_TAG:-latest}
    restart: unless-stopped
    env_file: .env
    ports:
      - "127.0.0.1:8004:8004"
    volumes:
      - ./data/hf-cache:/data/hf-cache
      - ./data/output:/data/output
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8004/health/ready"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
```

### Caddyfile (`deploy/Caddyfile`)

```caddyfile
tts-api.aitransclips.com {
	encode zstd gzip

	request_body {
		max_size 2MB
	}

	reverse_proxy 127.0.0.1:8004 {
		transport http {
			dial_timeout 2s
			response_header_timeout 90s
		}
	}

	log {
		output file /var/log/caddy/tts-api.access.log
		format json
	}
}
```

Notes:

1. Caddy obtains and renews certificates automatically once DNS points to the VPS and `80`/`443` are open.
2. The worker is published only on loopback (`127.0.0.1:8004`); host-systemd Caddy proxies to it. The container port is never exposed publicly.
3. The bind-mounted `./data` directories must be writable by uid 10001 (the container user); `provision.sh` handles this.
4. Tune `response_header_timeout` to match the worker's maximum synchronous processing time.

---

## 7. Implementation phases

### Phase 1 — repo and production baseline

1. Create the new repository.
2. Move the worker code and refactor it into the proposed structure.
3. Add Dockerfile, Compose file, Caddyfile, `.env.example`, CI workflow, and deploy workflow.
4. Implement HMAC middleware and replay protection.
5. Add `live` and `ready` health endpoints.
6. Provision the Singapore VPS (packages, ufw, Docker, Caddy, layout).
7. Add the Vercel DNS `A` record for `tts-api`.
8. Verify HTTPS with Caddy.

### Phase 2 — production hardening

1. Add image vulnerability scanning.
2. Add smoke tests in the deploy workflow.
3. Add disk and uptime monitoring.
4. Add structured request IDs end to end.
5. Tune concurrency from real usage.

### Phase 3 — only if needed later

1. Async job endpoints
2. Redis-backed nonce replay store
3. Blue/green deploy strategy
4. External object storage for generated audio
5. Centralized observability stack

---

## 8. Trade-offs

1. Hostname instead of raw IP
   - Slightly more setup because DNS is required
   - Much better TLS, rotation, and maintainability

2. HMAC instead of mTLS
   - Slightly less security than full mutual cert auth
   - Much lower operational burden and still strong for this trust boundary

3. Single VPS and single worker instance
   - Small downtime window during deploys
   - Lower cost and much simpler operations

4. Sync-first request model
   - Simpler integration now
   - May need async expansion later if job duration grows

## Bottom line

1. `tts-worker` in its own repository
2. containerized worker on a Singapore Ubuntu 22.04.5 VPS
3. `Caddy` on the host serving `https://tts-api.aitransclips.com`
4. worker reachable only through the reverse proxy on loopback (`127.0.0.1:8004`)
5. `HTTPS + HMAC-signed requests + timestamp + nonce` for authentication
6. GitHub Actions deploying immutable container images to the VPS with health-checked rollback

This gives a strong security baseline, low latency, a clean deployment story, and avoids the
fragility of IP-based trust.

# Monitoring and Maintenance

Day-one operational runbook for the `tts-worker` deployment.

## External uptime check (required)

Create an external monitor (UptimeRobot, Better Stack, or your provider's):

- URL: `https://tts-api.aitransclips.com/health/ready`
- Method: `GET`, expect `200`
- Interval: 1–5 minutes
- Alert: email / Slack / SMS

`/health/ready` verifies ffmpeg, the output dir, the model cache, signing keys, and
concurrency — so a green check means the worker can actually serve requests.

## Resource alerts (required)

In the VPS provider console, enable alerts for:

- CPU sustained high (e.g. > 85% for 10 min)
- Memory sustained high (e.g. > 85%)
- **Disk usage** — the most important one; warn at 80%, critical at 90%

Also add a disk-growth alert for the data volume:

```bash
du -sh /opt/tts-worker/data/hf-cache /opt/tts-worker/data/output
```

## TLS

Caddy renews certificates automatically. Optionally add an SSL-expiry monitor for
`tts-api.aitransclips.com` as a backstop.

## Log rotation

Docker json-file logs are capped by `deploy/docker-compose.yml` (`max-size: 10m`,
`max-file: 3`). Verify:

```bash
docker inspect --format '{{.HostConfig.LogConfig}}' tts-worker-tts-worker-1
ls -lh /var/lib/docker/containers/*/  | head
journalctl -u caddy --no-pager | tail
```

## Temp / output cleanup

Generated audio is streamed and deleted after each response; per-request workdirs are
removed in `generate_tts`'s `finally`. Verify the output volume stays empty:

```bash
docker exec tts-worker-tts-worker-1 ls -la /data/output
```

If outputs are ever retained intentionally, add a retention/cleanup job.

## Smoke test

`deploy/scripts/smoke.py` performs a signed `GET /v1/voices` and `POST /v1/tts`. It runs
automatically in the deploy workflow, and can be run manually on the VPS:

```bash
python3 /opt/tts-worker/scripts/smoke.py
```

## Maintenance cadence

- **Daily:** review the uptime monitor and any auth/upstream errors in Caddy + worker logs.
- **Weekly:** check disk usage of `data/hf-cache` and `data/output`.
- **Monthly:** apply host OS updates (`unattended-upgrades` handles security ones);
  review dependency updates.
- **Rotation:** rotate `HMAC_KEYS_JSON` with dual keys (add new key, deploy, switch the
  caller, then remove the old key).

## Useful commands

```bash
# container + health
docker compose -f /opt/tts-worker/docker-compose.yml ps
curl -s http://127.0.0.1:8004/health/ready

# logs
docker compose -f /opt/tts-worker/docker-compose.yml logs --tail=100 tts-worker
journalctl -u caddy --no-pager | tail -50

# manual rollback to the previous image tag
bash /opt/tts-worker/scripts/rollback.sh
```

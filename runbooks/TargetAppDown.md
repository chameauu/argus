# Runbook: TargetAppDown

## What it means
Prometheus has failed to scrape `target-app` for 1 minute: `up{job="target-app"} == 0`.
The app process, its port, or its network path is unhealthy; metrics, logs and traces from
it will all be missing.

## How to investigate
1. Prometheus targets `http://localhost:19090/targets` - read the scrape error for
   `target-app` (connection refused, timeout, 5xx, DNS).
2. Inspect the container:
   `docker compose -f docker-compose/docker-compose.yml ps target-app` and
   `docker compose -f docker-compose/docker-compose.yml logs target-app --tail=100`.
3. Hit the app directly: `curl -s http://localhost:18000/health` (and `/docs`).
4. If the container is up but unscrapeable, confirm it is on the `obs-net` network and that
   `/metrics` responds.

## Steps to resolve
- Container crashed: read the traceback, fix the cause, then
  `docker compose -f docker-compose/docker-compose.yml up -d target-app`.
- OOM-killed: raise its memory limit or fix the leak.
- Unreachable only from Prometheus: fix the service name/network wiring.
- Bad deploy: roll back to the last good image.

## How to silence during maintenance
```
docker compose -f docker-compose/docker-compose.yml exec alertmanager amtool silence add alertname="TargetAppDown" component="application" --duration=1h --comment="target-app restart / deploy"
```

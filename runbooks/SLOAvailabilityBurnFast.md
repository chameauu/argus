# Runbook: SLOAvailabilityBurnFast

## What it means
The `/work` availability objective is 99.5%, which allows 0.5% failures. This alert fires when
the burn rate exceeds 14.4x over **both** a 5-minute and a 1-hour window — that is more than
7.2% of requests failing, fast enough to consume 2% of a 28-day budget in one hour.

Both windows must agree, so a one-minute blip cannot page on its own.

## How to investigate
1. Grafana -> `argus-slo` dashboard -> availability and burn-rate panels to confirm the rate
   and see when it started.
2. Check whether a fault is injected (chaos run or a forgotten override):
   `curl -s http://localhost:18000/admin/fault`
3. Confirm status codes:
   `http://localhost:19090/api/v1/query?query=sum by (status) (rate(target_app_http_requests_total{endpoint="/work"}[5m]))`
4. Tempo -> `argus-traces` -> filter `status=error`; the failing span shows where it broke.
5. From the failing trace, jump to its Loki lines through the `trace_id` derived field.
6. Only `/work` counts toward this SLO; `/error-prone` is expected to fail and does not burn it.

## Steps to resolve
- Fault injected: `curl -X POST http://localhost:18000/admin/fault/reset`.
- Bad deploy: roll back `target-app` and watch the burn rate fall.
- Dependency failure: fix the dependency, then verify the 5m ratio recovers before closing.
- If the traffic source itself is broken (no requests), this is not an availability incident —
  check `up{job="target-app"}` and `target-app` logs instead.

## How to silence during maintenance
```
docker compose -f docker-compose/docker-compose.yml exec alertmanager amtool silence add alertname="SLOAvailabilityBurnFast" component="slo" --duration=1h --comment="planned maintenance on /work"
```

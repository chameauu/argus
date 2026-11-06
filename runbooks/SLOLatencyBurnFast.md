# Runbook: SLOLatencyBurnFast

## What it means
The `/work` latency objective is 95% of requests under 250ms, which allows 5% to be slower.
This alert fires when the burn rate exceeds 14.4x over both 5-minute and 1-hour windows — more
than 72% of requests are slower than 250ms.

## How to investigate
1. Grafana -> `argus-slo` dashboard -> latency compliance and burn-rate panels.
2. Check for injected delay: `curl -s http://localhost:18000/admin/fault`.
3. Confirm the percentile directly:
   `histogram_quantile(0.95, sum(rate(target_app_http_request_duration_seconds_bucket{endpoint="/work"}[5m])) by (le))`
4. Tempo -> find the slowest `/work` traces and read the span durations to separate application
   time from downstream time.
5. Check host pressure, since the workload and the monitoring stack share one machine:
   `node_load1`, `node_cpu_seconds_total{mode="idle"}`, and the node memory panels.

## Steps to resolve
- Injected delay: `curl -X POST http://localhost:18000/admin/fault/reset`.
- Real regression: roll back the deploy; if the cause is a dependency, add a timeout so slow
  calls fail fast instead of consuming the latency budget.
- Host saturation: reduce load-generator/k6 intensity or raise the container limits.
- Note that `/slow` has its own random delay and is not part of this SLO.

## How to silence during maintenance
```
docker compose -f docker-compose/docker-compose.yml exec alertmanager amtool silence add alertname="SLOLatencyBurnFast" component="slo" --duration=1h --comment="planned load test"
```

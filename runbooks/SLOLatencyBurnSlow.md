# Runbook: SLOLatencyBurnSlow

## What it means
The `/work` latency objective is 95% of requests under 250ms. This alert fires when the burn
rate stays above 6x for both a 30-minute and a 6-hour window — more than 30% of requests slower
than 250ms across six hours.

## How to investigate
1. Grafana -> `argus-slo` dashboard -> latency over 6h, next to the request-rate panel.
2. Separate constant slowness from occasional spikes: a flat p95 with a rising p99 points to a
   tail problem, usually a downstream dependency.
3. Compare with container memory and CPU, since sustained latency often follows throttling or GC
   pressure on the shared host.
4. Read the slowest traces in Tempo; the waterfall shows which span accumulates the time.
5. If the traffic profile changed (k6 spike or soak run), confirm whether that was expected.

## Steps to resolve
- Clear any injected delay first.
- Fix the slow path, then confirm the 30m window recovers; the 6h window clears gradually.
- If the objective itself is wrong for the workload, revisit the SLO instead of keeping an alert
  that fires permanently.

## How to silence during maintenance
```
docker compose -f docker-compose/docker-compose.yml exec alertmanager amtool silence add alertname="SLOLatencyBurnSlow" component="slo" --duration=4h --comment="known slow path under investigation"
```

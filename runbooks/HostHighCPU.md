# Runbook: HostHighCPU

## What it means
Host CPU usage has been above 85% for 5 minutes. The value is derived from
`node_cpu_seconds_total{mode="idle"}` scraped by node-exporter:
`100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100) > 85`.

## How to investigate
1. Grafana -> `argus-application` dashboard to see whether host load correlates with an
   application traffic/latency spike, or is host-only.
2. Query the offenders:
   `http://localhost:19090/api/v1/query?query=100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)`
3. Find the hot container with `docker stats` / `top` (candidates: target-app,
   custom-exporter, Prometheus, Loki, Tempo).
4. Check Prometheus `ALERTS` for related alerts (HostDiskFull, AppHighLatency) in the same window.

## Steps to resolve
- Identify and cap/restart the runaway container (`docker update --cpus=<n> <container>`).
- If the load is legitimate, give the host more CPU - it runs the whole observability stack.
- If it is a scrape/compaction burst (Prometheus/Loki/Tempo), stagger intervals or retention.
- If it is the target-app load-generator, this is expected demo traffic, not an incident.

## How to silence during maintenance
```
docker compose -f docker-compose/docker-compose.yml exec alertmanager amtool silence add alertname="HostHighCPU" component="host" --duration=1h --comment="planned maintenance"
```

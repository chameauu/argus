# Runbook: AppHighLatency

## What it means
`target-app` p99 request latency has exceeded 500ms for 3 minutes:
`histogram_quantile(0.99, sum(rate(target_app_http_request_duration_seconds_bucket[3m])) by (le)) > 0.5`.
Warning severity: users are waiting, but requests still succeed.

## How to investigate
1. Grafana -> `argus-application` dashboard -> latency panel (p95/p99) to see whether the
   slowdown is broad or limited to one endpoint.
2. Break it down by endpoint:
   `http://localhost:19090/api/v1/query?query=histogram_quantile(0.99, sum by (endpoint,le) (rate(target_app_http_request_duration_seconds_bucket[3m])))`
3. Tempo -> `argus-traces` dashboard -> open a slow trace and find the slowest span.
4. Jump from the slow trace to its Loki logs (`trace_id`) to see if a downstream call or a
   lock is slow.
5. Note `/slow` intentionally sleeps - check whether latency comes only from that endpoint.

## Steps to resolve
- One slow dependency: add a timeout/circuit-breaker, or fix the dependency.
- Bottlenecked database (or simulated DB): add an index or a cache.
- Under genuine load: scale out `target-app` or raise its concurrency.
- Only `/slow` demo traffic: expected - silence rather than "resolve."

## How to silence during maintenance
```
docker compose -f docker-compose/docker-compose.yml exec alertmanager amtool silence add alertname="AppHighLatency" component="application" --duration=1h --comment="expected from /slow demo traffic"
```

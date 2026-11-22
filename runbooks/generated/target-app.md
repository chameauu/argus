# Runbook: target-app

_Generated from `services/target-app.yaml` by `platform/generate.py`. Do not edit by hand._

## What it is
`target-app` (owner `jmal`, namespace `argus`, environment `local`) is an HTTP
service scraped at `target-app:8000/metrics` every 15s. It exposes 3 route(s):
`/work`, `/slow`, `/error-prone`.

## SLOs
- **Availability:** 99.5% of requests must not fail over
  30d (`status=~"5.."`).
- **Latency:** 95% of requests must complete in under 0.5 s.
- **Metrics:** counter `target_app_http_requests_total`, histogram `target_app_http_request_duration_seconds` (label `endpoint`).

## First three diagnostic steps
1. Open the generated dashboard: [dashboards/generated/red-target-app.json](../../dashboards/generated/red-target-app.json).
2. Query Loki for `{compose_service="target-app"}` over the last 15 minutes and look for errors
   around the first failing timestamp.
3. Copy a `trace_id=<32 lowercase hex>` from a Loki line into Tempo to find the failing span.

For a full investigation also check `up{job="target-app"}` in Prometheus, the container logs
(`docker compose -f docker-compose/docker-compose.yml logs target-app --tail=100`), and the
service's `/healthz` endpoint.

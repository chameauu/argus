# Runbook: foreign-app

_Generated from `services/foreign-app.yaml` by `platform/generate.py`. Do not edit by hand._

## What it is
`foreign-app` (owner `jmal`, namespace `argus`, environment `local`) is an HTTP
service scraped at `foreign-app:8099/metrics` every 15s. Its latency SLO covers
3 route(s): `/orders`, `/orders/{id}`, `/checkout`.

## SLOs
- **Availability:** 99.5% of requests must not fail over
  30d (`status=~"5.."`).
- **Latency:** 95% of requests must complete in under 0.5 s.
- **Metrics:** counter `orders_http_requests_total`, histogram `orders_http_request_duration_seconds` (label `route`).

## First three diagnostic steps
1. Open the service dashboard: [Argus — Service filtered to foreign-app](/d/argus-service?var-service=foreign-app).
2. Query Loki for `{compose_service="foreign-app"}` over the last 15 minutes and look for errors
   around the first failing timestamp.
3. Copy a `trace_id=<32 lowercase hex>` from a Loki line into Tempo to find the failing span.

For a full investigation also check `up{job="foreign-app"}` in Prometheus, the container logs
(`docker compose -f docker-compose/docker-compose.yml logs foreign-app --tail=100`), and the
service's `/healthz` endpoint.

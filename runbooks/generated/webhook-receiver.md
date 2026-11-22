# Runbook: webhook-receiver

_Generated from `services/webhook-receiver.yaml` by `platform/generate.py`. Do not edit by hand._

## What it is
`webhook-receiver` (owner `jmal`, namespace `argus`, environment `local`) is an HTTP
service scraped at `webhook-receiver:8080/metrics` every 15s. It exposes 3 route(s):
`/critical`, `/warning`, `/received`.

## SLOs
- **Availability:** 99.5% of requests must not fail over
  30d (`status=~"rejected|5.."`).
- **Latency:** 95% of requests must complete in under 0.5 s.
- **Metrics:** counter `webhook_received_total`, histogram `webhook_received_duration_seconds` (label `route`).

## First three diagnostic steps
1. Open the generated dashboard: [dashboards/generated/webhook-receiver.json](../../dashboards/generated/webhook-receiver.json).
2. Query Loki for `{compose_service="webhook-receiver"}` over the last 15 minutes and look for errors
   around the first failing timestamp.
3. Copy a `trace_id=<32 lowercase hex>` from a Loki line into Tempo to find the failing span.

For a full investigation also check `up{job="webhook-receiver"}` in Prometheus, the container logs
(`docker compose -f docker-compose/docker-compose.yml logs webhook-receiver --tail=100`), and the
service's `/healthz` endpoint.

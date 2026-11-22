# Runbook: custom-exporter

_Generated from `services/custom-exporter.yaml` by `platform/generate.py`. Do not edit by hand._

## What it is
`custom-exporter` (owner `jmal`, namespace `argus`, environment `local`) is an HTTP
service scraped at `custom-exporter:9200/metrics` every 15s. It exposes 0 route(s):
none.

## SLOs
- **Availability:** 99.5% of requests must not fail over
  30d (`status="failure"`).
- **Latency:** 95% of requests must complete in under 0.5 s.
- **Metrics:** counter `custom_exporter_http_requests_total`, histogram `custom_exporter_http_request_duration_seconds` (label `route`).

## First three diagnostic steps
1. Open the generated dashboard: [dashboards/generated/custom-exporter.json](../../dashboards/generated/custom-exporter.json).
2. Query Loki for `{compose_service="custom-exporter"}` over the last 15 minutes and look for errors
   around the first failing timestamp.
3. Copy a `trace_id=<32 lowercase hex>` from a Loki line into Tempo to find the failing span.

For a full investigation also check `up{job="custom-exporter"}` in Prometheus, the container logs
(`docker compose -f docker-compose/docker-compose.yml logs custom-exporter --tail=100`), and the
service's `/healthz` endpoint.

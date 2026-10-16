# Runbook: WebhookDeliveryFailing

## What it means
The simulated webhook delivery success ratio reported by `custom-exporter` has been below
50% for 30 seconds: `webhook_delivery_success_ratio < 0.5`. More than half of outbound
webhook deliveries are failing.

## How to investigate
1. Confirm the current value:
   `http://localhost:19090/api/v1/query?query=webhook_delivery_success_ratio`
   and `curl -s http://localhost:19200/metrics | grep webhook`.
2. Watch delivery latency: `webhook_delivery_duration_seconds` - a spike usually explains
   timeouts.
3. Check the receiving side: the webhook-receiver stub at `http://localhost:18090/health`
   and `http://localhost:18090/received` for errors or backlog.
4. This is a simulator - a low ratio may be an injected test override, not a real outage.

## Steps to resolve
- Test override is driving it: clear it with
  `curl -X POST http://localhost:19200/admin/reset`.
- Receiver down or slow: restore it, or add retries with exponential backoff.
- Failures are HTTP 4xx from the receiver: fix the payload or the URL.
- Failures are 5xx/timeouts: add a queue and a dead-letter path so deliveries retry.

## How to silence during maintenance
```
docker compose -f docker-compose/docker-compose.yml exec alertmanager amtool silence add alertname="WebhookDeliveryFailing" component="custom-exporter" --duration=1h --comment="degraded delivery path / maintenance"
```

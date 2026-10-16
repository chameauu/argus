# Runbook: CustomExporterQueueHigh

## What it means
The simulated job queue depth reported by `custom-exporter` has been above 100 for 30
seconds: `job_queue_depth > 100`. Workers are not draining the queue as fast as jobs arrive,
so processing is falling behind.

## How to investigate
1. Watch the trend in Grafana (`argus-application`) or Prometheus:
   `http://localhost:19090/api/v1/query?query=job_queue_depth`
2. Compare arrivals with processed count:
   `rate(jobs_queued_total[5m])` and `sum by (status) (rate(jobs_processed_total[5m]))`.
3. Confirm the exporter is healthy and being scraped: `http://localhost:19090/targets`
   (job `custom-exporter`), and `curl -s http://localhost:19200/metrics | grep job_queue_depth`.
4. This is a simulator - a high value may be an injected test override rather than real load.

## Steps to resolve
- Test override is driving it: clear it with
  `curl -X POST http://localhost:19200/admin/reset`.
- Real backlog: add workers or increase the drain rate; scale the consumer.
- A stuck/poison job is blocking the queue: find and drop it.
- Exporter itself wedged: `docker compose -f docker-compose/docker-compose.yml restart custom-exporter`.

## How to silence during maintenance
```
docker compose -f docker-compose/docker-compose.yml exec alertmanager amtool silence add alertname="CustomExporterQueueHigh" component="custom-exporter" --duration=1h --comment="queue drain / maintenance"
```

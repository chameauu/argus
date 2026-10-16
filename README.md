# Argus

A production-style observability platform implementation built with the LGTM stack and designed for end-to-end telemetry validation.

The project delivers a complete, reproducible local environment for metrics, logs, traces, dashboards, and alerting workflows, with verification-first engineering practices.

## Implementation roadmap

1. Metrics foundation
2. Logs pipeline
3. Traces pipeline
4. Grafana dashboards and correlation
5. Alerting and webhooks
6. Advanced SLOs, load testing, chaos validation, and CI

## Quick start

```bash
cd docker-compose
docker compose up --build -d
```

Then open:

- Prometheus: http://localhost:19090
- Grafana: http://localhost:13000 (admin / changeme_local_only)
- Loki: http://localhost:13100
- Target app: http://localhost:18000/docs
- Webhook receiver: http://localhost:18090/received

## Platform architecture (baseline)

- Prometheus scrapes platform and application metrics
- `target-app` provides traffic, error, and latency behavior for realistic telemetry
- `custom-exporter` publishes business-focused Prometheus metrics
- Grafana loads datasources and dashboards from code
- `webhook-receiver` validates alert routing end-to-end

## Current delivery status

Current implementation includes:

- Docker Compose orchestration
- Prometheus scrape configuration
- FastAPI target workload service
- Custom Python metrics exporter
- Loki + Promtail log pipeline
- Webhook receiver service for alert testing
- Grafana provisioning for Prometheus and Loki datasources

## Engineering approach

- Build incrementally by milestones
- Verify each stage before expansion
- Capture evidence under `docs/evidence/`
- Keep configs, dashboards, and alerts fully versioned as code

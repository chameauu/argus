# Argus

A production-style observability platform implementation built with the LGTM stack and designed for end-to-end telemetry validation.

The project delivers a complete, reproducible local environment for metrics, logs, traces, dashboards, and alerting workflows, with verification-first engineering practices.

## Implementation roadmap

1. Metrics foundation
2. Logs pipeline
3. Traces pipeline
4. Grafana dashboards and correlation
5. Alerting and webhooks
6. Baseline hardening (healthchecks, retention, resource limits)
7. SLOs, load testing, chaos validation, and CI (complete)

## Quick start

```bash
cd docker-compose
docker compose up --build -d
```

Then open:

- Prometheus: http://localhost:19090
- Alertmanager: http://localhost:19093
- Grafana: http://localhost:13000 (admin / changeme_local_only)
- Loki: http://localhost:13100
- Tempo: http://localhost:13200
- Target app: http://localhost:18000/docs
- Webhook receiver: http://localhost:18090/received

Wait until every healthchecked service reports `healthy`:

```bash
docker compose ps
```

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
- Loki + Alloy log pipeline
- Tempo + OpenTelemetry Collector trace pipeline
- Grafana provisioning for Prometheus, Loki, and Tempo datasources
- Trace-to-log and trace-to-metric correlation
- Dashboards provisioned as code
- Prometheus alert rules + Alertmanager routing to a local webhook receiver
- Per-alert runbooks under `runbooks/`
- Healthchecks, memory limits, and 7-day retention on Prometheus, Loki, and Tempo
- SLOs as code for `/work` (99.5% availability, 95% under 250ms) with multi-window
  burn-rate alerts and an SLO dashboard
- k6 load profiles and deterministic chaos experiments that assert alerts fire and clear

## Operations and troubleshooting

### Service health

```bash
cd docker-compose
docker compose ps
docker inspect --format '{{json .State.Health}}' argus-prometheus-1
```

Ten services expose a Docker healthcheck. Two intentionally do not:
`load-generator` is a traffic loop with no HTTP server, and `otel-collector` is a
distroless image with no shell, so Docker cannot exec a probe inside it. The collector
exposes a real health endpoint instead:

```bash
curl -s http://localhost:13133/health/status
```

### Prometheus is up but shows no alerts

Check that the rules were actually loaded and that Alertmanager is discovered:

```bash
curl -s http://localhost:19090/api/v1/rules | jq '.data.groups[].rules[] | .name'
curl -s http://localhost:19090/api/v1/alertmanagers | jq '.data.activeAlertmanagers'
```

Alert timing is not just `for:`. Prometheus must evaluate the rule, Alertmanager applies
`group_wait` before the first notification, and `group_interval` between updates. Expect
30-90 seconds from trigger to webhook delivery.

### Alerts can be tested deterministically

`custom-exporter` exposes override endpoints, so no random load is needed:

```bash
curl -X POST 'http://localhost:19200/admin/override?job_queue_depth=250'
curl -X POST 'http://localhost:19200/admin/override?webhook_delivery_success_ratio=0.1'
curl -s http://localhost:18090/received | jq
curl -X POST http://localhost:19200/admin/reset
```

### `loki` or `tempo` is unhealthy right after startup

Both return 503 from `/ready` while their ingesters stabilize, typically for 30-70 seconds.
Their healthchecks use a 60-second `start_period` so this is expected, not a failure.
Do not restart them during that window.

### Grafana does not show a datasource change

Grafana provisioning reads configuration at startup. Changing provisioning files and
restarting is enough for new datasources, but existing provisioned datasources are not
updated in place. Reset Grafana's state to force re-provisioning:

```bash
docker compose rm -sf grafana
docker volume rm argus_grafana-data
docker compose up -d grafana
```

This also deletes local Grafana state; dashboards and datasources are recreated from code.

### `tempo` crashes with a permission error on `/data/tempo`

The Tempo image runs as uid 10001 while the named volume is root-owned. This stack runs
Tempo as root locally (`user: "0:0"`). In Kubernetes, set `securityContext.fsGroup`
instead of running as root.

### Prometheus fails to start with a read-only mount error

Do not mount alert rules *inside* the read-only Prometheus config directory. Docker cannot
create a mountpoint under a read-only bind. Rules are mounted at `/etc/prometheus-rules`
and referenced from `rule_files`.

### Logs are missing from Loki

Alloy is scoped to this Compose project. Confirm the filter and Alloy's own readiness:

```bash
docker compose logs alloy --tail=50
curl -s http://localhost:13100/ready
```

A container started outside the `argus` Compose project is intentionally not ingested.

### Disk usage keeps growing

Retention is capped at 7 days for metrics (Prometheus `7d`), logs (Loki `168h`), and traces
(Tempo `block_retention: 168h`). If the host disk still fills, inspect the data volumes
first:

```bash
docker system df
docker volume inspect argus_tempo-data
```

Never delete a live Prometheus TSDB or Tempo WAL directory.

## SLOs

Two objectives are defined for `/work` only, because `/error-prone` fails by design:

| Objective | Target | Budget |
|---|---|---|
| Availability | 99.5% of requests are non-5xx | 0.5% |
| Latency | 95% of requests complete under 250ms | 5% |

`alert-rules/slo-rules.yml` records each SLI at 5m/30m/1h/6h and alerts on the standard
multi-window burn rates: 14.4x over 5m and 1h (critical), 6x over 30m and 6h (warning). The
`argus-slo` dashboard shows both objectives, the remaining budget, and the burn rate with the
thresholds drawn in.

## Load testing and chaos

k6 profiles run inside the Compose network, so they resolve `target-app` by name and need no
published ports:

```bash
bash k6/run.sh smoke      # 5 requests, sanity check
bash k6/run.sh steady     # 10 req/s for 2 minutes
bash k6/run.sh spike      # 2 -> 60 VUs
bash k6/run.sh soak       # 5 VUs for 30 minutes
```

Chaos experiments inject a fault, assert the expected alert fires, clear it, and assert
recovery:

```bash
bash chaos/queue-backlog.sh          # CustomExporterQueueHigh
bash chaos/kill-target-app.sh        # TargetAppDown
bash chaos/slo-availability-burn.sh  # SLOAvailabilityBurnFast
bash chaos/slo-latency-burn.sh       # SLOLatencyBurnFast
bash chaos/run-all.sh                # all four, roughly 15 minutes
```

The SLO experiments inject failures or delay into `/work` through
`POST /admin/fault?error_rate=0.95` (or `delay_seconds=0.5`) and clear it with
`POST /admin/fault/reset`. Recovery is asserted on the 5-minute SLI rather than on the alert,
because a 1-hour window keeps a burn-rate alert pending long after the fault is gone.

## Engineering approach

- Build incrementally by milestones
- Verify each stage before expansion
- Capture evidence under `docs/evidence/`
- Keep configs, dashboards, and alerts fully versioned as code

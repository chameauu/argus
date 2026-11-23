# Argus

A local observability platform built on the LGTM stack, where **observability for a service is generated from one descriptor file** rather than hand-written per service.

Fourteen containers, one host. Metrics, logs, traces, dashboards, SLOs and alerting, all configured as code, with a conformance checker that can reject a service instead of quietly accepting it.

## Quick start

```bash
cd docker-compose
docker compose up --build -d
docker compose ps            # 14 running, 12 healthy
```

| Service | URL | Notes |
|---|---|---|
| Grafana | http://localhost:13000 | `admin` / `changeme_local_only` |
| Prometheus | http://localhost:19090 | |
| Alertmanager | http://localhost:19093 | |
| Loki | http://localhost:13100 | no UI of its own; query via Grafana |
| Tempo | http://localhost:13200 | no UI of its own |
| OTel Collector | http://localhost:13133/health/status | collector health |
| Target app | http://localhost:18000/docs | Swagger UI |
| Foreign app | http://localhost:18099/orders | the app the platform did not write |
| custom-exporter | http://localhost:19200/metrics | business metrics + fault overrides |
| webhook-receiver | http://localhost:18090/received | alert payloads |
| node-exporter | http://localhost:19100/metrics | host metrics |
| cAdvisor | http://localhost:18080/metrics | container memory |

Scrape targets: **9 up** — prometheus, node-exporter, cadvisor, alloy, alertmanager, custom-exporter, target-app, webhook-receiver, foreign-app.

## How the platform works

Two tiers, and the distinction is the honest one to make when describing this project.

**Tier A — attach. No platform edit.** Point an application at the collector and name it:

```bash
OTEL_SERVICE_NAME=my-app
OTEL_EXPORTER_OTLP_ENDPOINT=http://<host>:4318     # 4317 for gRPC
```

Traces, metrics and logs land. The collector drops anything without `service.name` (silently at
the SDK, which is why `otlp_service_name` probes the store rather than trusting the response),
stamps `service` from the resource, and derives RED from spans via the `spanmetrics` connector.
The app appears in the `$service` selector on `Argus — Service` with request rate, error ratio and
p95 computed from its spans — no naming convention, no descriptor, no pull request.

`bash platform/attach_proof.sh <image> <service>` proves this end-to-end: it starts a container
outside the repo with two env vars, drives traffic, and fails unless metrics, traces, logs and the
span-derived RED row all materialise **and** `git status` is unchanged. Last run: `span-derived
calls 12, 5 traces, 5 log lines, selectable in Argus — Service`.

**Tier B — curate. One descriptor.** SLOs, burn-rate pages and runbooks are claims about your
service that telemetry cannot invent, so they are declared:

```yaml
# services/webhook-receiver.yaml — minimal form
name: webhook-receiver
owner: jmal
namespace: argus
environment: local
scrape: {port: 8080, path: /metrics, interval: 15s}
health: {path: /healthz}
runbook: runbooks/WebhookDeliveryFailing.md
```

`metrics`, `slos` and `routes_allowlist` are optional enrichment. A descriptor without `metrics`
gets a scrape job, a `Down` alert and a runbook that points at span-derived RED; one with `metrics`
additionally gets RED error/latency rules and Sloth SLO rules:

```yaml
metrics: {requests_total: orders_http_requests_total, route_label: route}
slos:
  availability: {objective: 0.995, window: 30d, error_selector: 'status=~"5.."'}
  latency: {objective: 0.95, threshold_seconds: 0.5, histogram: orders_http_request_duration_seconds}
routes_allowlist: [/orders, /orders/{id}, /checkout]
```

`python3 platform/generate.py` renders, deterministically, from every descriptor:

| Artefact | Path | Emitted when |
|---|---|---|
| Scrape jobs (whole file) | `docker-compose/configs/prometheus/prometheus.yml` | always |
| RED alerts | `alert-rules/generated/red-<name>.yml` | always (`Down` only without `metrics`) |
| SLO recording + burn-rate rules (via Sloth) | `alert-rules/generated/slo-<name>.yml` | only with `metrics` |
| Runbook stub | `runbooks/generated/<name>.md` | always |

The dashboard is **not** generated per service — it is one hand-maintained dashboard
(`dashboards/argus-service.json`) with a `$service` variable, so thirteen services and three
hundred would use the same file.

Generated files carry a do-not-edit header and are never hand-edited. Four gates keep that true:

```bash
python3 platform/generate.py --check           # drift, orphans, Compose services without a descriptor
bash platform/contract_live.sh                 # config parity + 4 platform checks + 40/40 conformance
bash platform/test_rules.sh                    # 3 stdlib golden suites + 3 promtool unit tests
python3 platform/config_parity.py              # running config == committed config (in contract_live too)
```

`config_parity.py` exists because the collector has no hot reload: an edit that is never restarted
leaves the running container on old config, and a revert after a restart leaves the committed file
lying. The first direction is caught by mtime versus container start; the second by the
`span_metrics` check, which only passes if the committed config actually produces span-derived RED.

### Onboarding a service — the honest version

**Attach only (Tier A):** set two environment variables; nothing in this repository changes. Run
`attach_proof.sh` to verify before telling anyone it works.

**Attach + curate (Tier B):**

1. Write `services/<name>.yaml` — seven keys if you only want a scrape job and a `Down` alert.
2. Add a Compose block for the container. **This is still a manual edit** — the descriptor does not
   generate Compose services, and making it do so is the next structural step.
3. `python3 platform/generate.py`, then reload Prometheus (`docker kill -s HUP argus-prometheus-1`).
4. `bash platform/contract_live.sh` — all checks must pass.

Measured once end-to-end on a Flask service written from the contract alone: **165 seconds**,
descriptor in hand to 36/36 green on a warm stack. That number excludes writing the app itself, and
it required one generator bug fix (route templates such as `/orders/{id}` were double-escaped for
PromQL). Say "descriptor to observability in 165 s", not "onboarding took 165 s".

## The contract

`platform/contract/v1.md` defines five rules. Each is machine-checked:

1. **Telemetry** — OTLP to the collector with `service.name`, `service.namespace`, `deployment.environment`.
2. **Metrics** — `/metrics` with a request counter (route + status) and a latency histogram in seconds, route labels templated (`/orders/{id}`, never raw paths), every series carrying `service`.
3. **Health** — `/healthz` returns 200 in under a second with no downstream calls.
4. **Correlation** — logs carry `trace_id=<32 lowercase hex>` on the request line.
5. **Descriptor** — `services/<name>.yaml`, schema v1.

The rules split along the two tiers. Rules 1, 3 and 4 are the **attach** contract: send OTLP with a
service name, answer `/healthz`, put `trace_id` on request log lines — and you are visible. Rule 2 is
the **curation** contract: a request counter and latency histogram in a naming you declare in the
descriptor, which is what makes error-rate and latency rules and SLOs possible. Rule 5 is the
descriptor itself, and it is optional: without one you get everything Tier A provides and nothing
Tier B does.

Two things about the contract are deliberate:

- **The platform stamps `service` at scrape time** via `metric_relabel_configs`. Applications do not have to label anything, and the checker reports the difference truthfully.
- **`/admin/*` and `/health*`, `/metrics`, `/docs` are infrastructure routes**, exempt from the route allowlist. They are excluded rather than listed because the allowlist also filters the latency SLI — control-plane traffic must not enter the SLO.

`contract_live.sh` runs ten checks per service: `metrics_reachable`, `service_label`, `route_label`, `series_budget`, `series_integrity`, `healthz`, `otlp_traces`, `trace_id_log_format`, `log_continuity`, `descriptor`.

Four checks run once for the platform rather than per service: `config_parity` (running config equals
committed config), `otlp_metrics` (a real OTLP/HTTP metric lands in Prometheus), `otlp_service_name`
(telemetry without `service.name` is dropped), `span_metrics` (the collector is deriving RED from
spans). `span_metrics` is the one that would have caught the day this repo ran a spanmetrics connector
in a container but not in git.

## Alerts and SLOs

Three kinds, deliberately separated:

| Kind | Source | Examples |
|---|---|---|
| Generated RED | `platform/templates/red-rules.yml.tmpl` | `TargetAppDown`, `TargetAppHighErrorRate`, `TargetAppHighLatency` |
| Generated SLO | Sloth, per descriptor | `TargetAppAvailabilityBurnRate`, `TargetAppLatencyBurnRate` |
| Hand-written | `alert-rules/rules.yml`, `alert-rules/platform.yml` | host CPU/disk, exporter queue, platform self-monitoring |

Every service gets the same two objectives unless its descriptor says otherwise:

| Objective | Target |
|---|---|
| Availability | 99.5% of requests non-5xx |
| Latency | 95% of requests under 0.5 s |

Burn-rate alerting uses Sloth's multi-window multi-burn construction, so it needs baseline: a fresh service cannot page in fifteen minutes, and clearing a bad historical window takes as long as that window.

### Quiet by default

A healthy stack has exactly one firing alert: `Watchdog`, which is a dead-man's switch and is supposed to fire. Everything else must be *caused*:

```bash
curl -X POST 'http://localhost:18000/admin/fault?error_rate=0.8'     # errors → RED + SLO page
curl -X POST 'http://localhost:18000/admin/fault?delay_seconds=1.0' # latency → RED latency alert
curl -X POST 'http://localhost:19200/admin/override?job_queue_depth=250'
curl -X POST 'http://localhost:19200/admin/override?webhook_delivery_seconds=0.9'
curl -X POST http://localhost:18000/admin/fault/reset
curl -X POST http://localhost:19200/admin/reset
```

The load generator sends a healthy mix by design (98% `/work`, 2% `/error-prone`). `/slow` is out of the steady mix because the generated latency rule groups per endpoint — enabling it makes `endpoint=/slow` page forever. Exercise it on demand with `k6/slow.js` or `LOAD_GENERATOR_SLOW_EVERY=N`.

### Platform self-monitoring

`alert-rules/platform.yml` watches the platform itself:

- `PlatformTargetDown` — any scrape target down for 5 minutes
- `PlatformScrapeSamplesDrop` — scrape volume collapses by 20%
- `PlatformTSDBHighSeries` — head series above 11000 (measured baseline ~7500)
- `PlatformRuleEvaluationFailing` — the rule engine is erroring
- `PlatformAlertDeliveryFailing` — Alertmanager cannot deliver
- `ContainerMemoryHigh` — a container above 85% of its memory limit for 10 minutes
- `Watchdog` — always firing, the dead-man's switch

`Watchdog` has no external receiver yet: it proves Prometheus is evaluating, not that a human would be woken.

## Retention and capacity

| Signal | Retention |
|---|---|
| Prometheus | 30 days **or 2 GiB**, whichever comes first |
| Loki | 168 hours |
| Tempo | 168 hours |

The 30-day SLO window needs 30 days of samples, so retention was raised from 7 days. The 2 GiB cap was sized from measurement, not guesswork: 3018 series, 199.8 samples/s, 1.32 bytes/sample → ~684 MB per 30 days, doubled for index and WAL, against 71 GB free.

**Span-derived RED costs series too.** The `spanmetrics` connector emits `calls` plus a duration
histogram per dimension set: measured **400 `spanmetrics_*` series for four services** (20 logical
series each, 17 histogram buckets included), against a head of ~5 400. Two consequences worth knowing:

- `span_name` is added automatically and is bounded only if applications name spans after the route
  (`GET /orders/{id}`), not after the instance (`GET /orders/1234`). An app that puts IDs in span names
  multiplies cardinality; the fix is `exclude_dimensions: [span.name]` when that happens — measured
  cost first, not assumed.
- Connector state is cumulative: a service's series persist after its container is gone until
  `metrics_expiration` or a collector restart clears them.

**Cardinality guard.** Every generated scrape job keeps only the labels the platform understands:

```
__name__|service|route|endpoint|status|le|quantile|job|instance|version|generation|type|mode|code|method|handler
```

Anything else is dropped before it reaches the TSDB. Do not "simplify" this list: removing a label that distinguishes otherwise-identical series does not reduce cardinality, it silently merges series — Prometheus keeps one sample per timestamp and discards the rest without an error. That happened here (`python_gc_collections_total` lost `generation`: 12 series became 4), and it is why `series_integrity` exists: it compares the label sets a service exposes against what Prometheus actually stored, and fails naming the lost label.

## Operations and troubleshooting

### Service health

```bash
cd docker-compose
docker compose ps
```

Twelve of fourteen containers report `healthy`. Two intentionally do not: `load-generator` is a traffic loop with no HTTP server, and `otel-collector` is distroless with no shell to exec a probe into — it exposes a real health endpoint instead:

```bash
curl -s http://localhost:13133/health/status
```

### Prometheus is up but shows no alerts

```bash
curl -s http://localhost:19090/api/v1/rules | jq '.data.groups[].rules[] | .name'
curl -s http://localhost:19090/api/v1/alertmanagers | jq '.data.activeAlertmanagers'
```

Timing is not just `for:`. Prometheus evaluates, then Alertmanager applies `group_wait` before the first notification and `group_interval` between updates: expect 30–90 seconds from trigger to webhook delivery.

### A generated file says it was edited

`platform/generate.py --check` names it. Regenerate — do not hand-edit, and do not "fix" the file the gate is complaining about.

### `loki` or `tempo` is unhealthy right after startup

Both return 503 from `/ready` while ingesters stabilise, typically 30–70 seconds. Healthchecks have a 60-second `start_period`; do not restart them in that window.

### Grafana does not show a provisioning change

Provisioned datasources are not updated in place when the file's `version:` is lower than the stored row. Bump the version, or reset Grafana's state (this deletes local Grafana data; dashboards and datasources are recreated from code):

```bash
docker compose rm -sf grafana
docker volume rm argus_grafana-data
docker compose up -d grafana
```

### `tempo` fails on `/data/tempo`

The image runs as uid 10001 while the named volume is root-owned; this stack runs Tempo as root locally (`user: "0:0"`). In Kubernetes use `securityContext.fsGroup` instead.

### Prometheus fails with a read-only mount error

Do not mount rules *inside* the read-only config directory. Rules live at `/etc/prometheus-rules` and are referenced from `rule_files`.

### Logs are missing from Loki

Alloy is scoped to this Compose project:

```bash
docker compose logs alloy --tail=50
curl -s http://localhost:13100/ready
```

On a fresh `alloy-data` volume Alloy replays every container's log from the beginning; `loki.process` drops entries older than one hour, and `loki_process_dropped_lines_total{reason="replay"}` counts them. That volume is load-bearing — a replay storm is normal, deletion of the volume mid-incident is not.

### Disk usage keeps growing

```bash
docker system df
docker volume inspect argus_prometheus-data
```

Never delete a live Prometheus TSDB or Tempo WAL directory.

## Load testing and chaos

k6 profiles run inside the Compose network and need no published ports:

```bash
bash k6/run.sh smoke      # 5 requests
bash k6/run.sh steady     # 10 req/s for 2 minutes
bash k6/run.sh spike      # 2 -> 60 VUs
bash k6/run.sh soak       # 5 VUs for 30 minutes
bash k6/run.sh slow       # exercise /slow and /error-prone on demand
```

`chaos/` injects a fault, asserts an alert fires, clears it, and asserts recovery. Caveat: the two SLO scripts still assert the pre-cutover rule names (`SLOAvailabilityBurnFast`, `SLOLatencyBurnFast`), which the generated Sloth rules replaced (`TargetAppAvailabilityBurnRate`). The kill and queue scripts assert rules that still exist. Treat the SLO chaos scripts as needing an update, not as passing.

## Repository layout

```
platform/            generator, contract, checker, tests  (tracked)
services/            one descriptor per service           (tracked)
alert-rules/         hand-written rules + generated/      (tracked)
dashboards/          hand-built + generated/              (tracked)
runbooks/            per-alert runbooks + generated/      (tracked)
docker-compose/      stack + configs (prometheus.yml generated)
target-app/          instrumented FastAPI demo
foreign-app/         Flask app written from the contract alone
foreign-app-bad/     deliberately non-conforming fixture
k6/ chaos/           load profiles and fault experiments
docs/                labs, council reports, plans (gitignored)
```

## Documentation

`docs/` is gitignored by design — it holds study material, not build inputs:

- `docs/labs/` — 24 files, three per component: why it exists, how to break it, how to see it. Every command was executed against this stack.
- `docs/council/` — six rounds of five-advisor review with peer review, including the ones that found the cardinality guard silently deleting data and the route gate that could not fail.
- `docs/platform/PLAN.md`, `ARCHITECTURE.md`, `adr-0001-log-agent.md` — the plan of record and why Alloy replaced Promtail.

## Engineering approach

- Verify each stage before expanding; every claim in this README was measured.
- Prefer a gate that fails on purpose over a green check nobody has seen go red.
- Generated artefacts are never hand-edited; drift and orphans are CI failures.
- Name the limits: single host, single operator, HTTP services only, no multi-tenancy.

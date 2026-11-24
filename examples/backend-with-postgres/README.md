# backend-with-postgres — the same platform, two different integrations

A working two-app example: an HTTP API and the PostgreSQL database behind it.
The API joins Argus by attaching; the database joins by being scraped. That
difference is the point of the example — it is the line between Tier A and
Tier B in the platform README.

```
browser/curl ──▶ backend (FastAPI + psycopg2) ──▶ postgres
                      │                              │
                      │ OTLP :4318                   │ SQL
                      ▼                              ▼
              otel-collector ──▶ Tempo            postgres-exporter :9187
                      │                              │ /metrics
                      └──▶ Prometheus ◀──── scrape ──┘
```

## Containers

| Service | Image | Role |
|---|---|---|
| `postgres` | `postgres:16-alpine` | The database. Owns `orders`; no telemetry of any kind. |
| `postgres-exporter` | `prometheuscommunity/postgres-exporter:v0.15.0` | Connects to postgres and exposes `pg_up` and the `pg_*` server metrics on :9187. |
| `backend` | built from `backend/` | FastAPI + psycopg2. Serves `/health`, `/orders`, `/orders/{id}` and `POST /orders`. |

All three join the **external** network `argus_obs-net`, so they resolve
`otel-collector` and are resolvable by Prometheus without touching the argus
Compose project. The Compose project name is pinned to `demo-pair` so `down`
cannot reach `argus-*` containers.

## The two tiers this demonstrates

**Backend — Tier A, attach, no platform edit.** The backend is given four
environment variables and nothing else:

```
OTEL_SERVICE_NAME=demo-backend
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
DATABASE_URL=postgresql://postgres:pw@postgres:5432/orders
```

There is **no descriptor for the backend**. The collector takes `service` from
the OTLP resource, the `spanmetrics` connector derives RED from the FastAPI
server spans, and the service becomes selectable on `Argus — Service`. Adding a
descriptor would be extra curation, not something the attach path needs.

**Database — Tier B, scrape + descriptor.** PostgreSQL cannot emit OTLP: it has
no OpenTelemetry SDK, no exporter loop and no resource attributes. A database is
not an application that can be handed two env vars. The integration is
therefore a bridge plus a declaration:

1. `postgres-exporter` translates `pg_stat_*` into Prometheus text on :9187.
2. `services/demo-postgres.yaml` (in this directory; installed by `run.sh`)
   tells the generator to scrape `demo-postgres:9187` every 30 s, to name the
   service `demo-postgres`, and to emit a `DemoPostgresDown` alert and a
   runbook. No `metrics` block means no SLO file — a database has no request
   counter or latency histogram, so inventing one would be a lie.
3. The `service="demo-postgres"` label is applied by the **generator's
   `metric_relabel_configs`**, not by the exporter. The exporter does not know
   the platform name.

The descriptor's `name` must also be the exporter's network alias
(`demo-postgres` in `docker-compose.yml`). The generated scrape target is
`<name>:<scrape.port>`, and Compose would otherwise publish only the service
name `postgres-exporter` — the target would never resolve and `up` would stay 0
while every container looked healthy.

## Run

```bash
bash examples/backend-with-postgres/run.sh
```

The script:

1. checks `argus_obs-net` exists and the platform is up;
2. installs `services/demo-postgres.yaml` from this directory;
3. runs `python3 platform/generate.py` and SIGHUPs Prometheus;
4. `docker compose up -d --build`, waits for all three containers to be
   healthy, and drives `GET /orders` ×5, `GET /orders/1` ×5 and
   `POST /orders` ×2;
5. waits up to 60 s for the backend's spans and the database's scrape to reach
   Prometheus, prints the results and fails if any of them is missing.

The API is published at <http://localhost:18001>; try
`curl localhost:18001/orders`. The exporter is published at
<http://localhost:19187/metrics> only for inspection — Prometheus scrapes it on
the Docker network at `demo-postgres:9187`.

```bash
bash examples/backend-with-postgres/stop.sh
```

`stop.sh` runs `docker compose down -v`, verifies no `demo-pair` container is
left, removes the installed descriptor, regenerates/prunes the artefacts and
reloads Prometheus. The argus repository is left as it was.

## The three verification queries

Run these against Prometheus (<http://localhost:19090>) after `run.sh`:

```promql
# 1. Tier A: the backend attached. Span-derived calls exist for its service.
sum(spanmetrics_calls_total{service="demo-backend"})

# 2. Tier B: the database's scrape target is up.
up{job="demo-postgres"}

# 3. Tier B: the exporter can actually reach PostgreSQL (not just "up").
pg_up{service="demo-postgres"}
```

Expected: a positive number, `1`, and `1`. A target that is `up` with
`pg_up == 0` is the interesting failure — the exporter process is alive but the
database is not reachable, which is why the descriptor's `Down` alert alone is
not enough.

Two supporting checks:

```bash
# The backend is selectable in the dashboard's $service variable.
curl -s localhost:19090/api/v1/label/service/values | jq -r '.data[]' | grep demo-backend

# The database exposes series under the generated service label.
curl -s --get --data-urlencode 'query=count({service="demo-postgres"})' \
  localhost:19090/api/v1/query | jq -r '.data.result[0].value[1]'
```

## Trace → database correlation

The database integration is metrics-only, but a request trace still shows the
database hop because the **client library** is instrumented:

```bash
# Find a backend trace, then fetch it and look for the child span.
curl -s --get --data-urlencode 'q={resource.service.name="demo-backend"}' \
  --data-urlencode 'limit=5' localhost:13200/api/search | jq -r '.traces[0].traceID'
```

The FastAPI server span (for example `GET /orders`) has a psycopg2 `CLIENT`
child whose `db.system` is `postgresql`. That child is emitted by
`Psycopg2Instrumentor` in the app, not by PostgreSQL. The database itself is
still invisible to Tempo; only the client's view of the call is traced.

Observed on this stack (`opentelemetry-instrumentation-psycopg2` 0.50b0 with
`psycopg2-binary` 2.9.10), the client span carries:

```
db.system=postgresql   db.name=orders   db.user=postgres
db.statement="SELECT id, item, created_at FROM orders ORDER BY id"
net.peer.name=postgres net.peer.port=5432
```

Neither `peer.service` nor `db.connection_string` is emitted — not redacted,
simply absent. The statement is present without the bound parameter values
(`WHERE id = %s` stays parameterised), so no row data leaks into Tempo.

```bash
curl -s "localhost:13200/api/traces/<traceID>" |
  jq -r '.batches[].scopeSpans[].spans[] | select(.attributes[]?.key=="db.system") |
         {name, attributes: [.attributes[] | {key, value}]}'
```

## Why the backend has no descriptor, and the database needs one

Tier A derives everything the dashboard needs from spans. A descriptor would
only add curation (declared metric names, SLOs, a route allowlist) — and the
backend deliberately exports its own OTel metrics rather than a `/metrics`
endpoint, so it has no scrape target to describe. The database is the opposite:
it produces no spans and no OTLP resource, so the only way to see it is a
scrape target, and a scrape target is what a seven-key descriptor exists to
generate. There is no env-var path for "a database that instruments itself".

## Traps for someone repeating this

- **The exporter tag in the brief was imaginary.** `postgres-exporter:v0.15.2`
  is not published on Docker Hub or Quay; the tags go `v0.15.0` → `v0.16.0`.
  This example uses `v0.15.0`, the nearest real release. Docker fails the pull
  before starting anything, so the error is loud rather than silent.
- **Descriptor name = network alias.** `demo-postgres` in the YAML must resolve
  on `argus_obs-net`. Without the Compose `aliases:` entry, `up` is 0 forever.
- **`psycopg2-binary==2.9.9` does not install on Python 3.13.** It publishes no
  cp313 wheel and its sdist fails to compile against 3.13's headers. This
  example keeps Python 3.13 and pins `2.9.10` (the patch release that added the
  cp313 wheel); `2.9.9` works only with Python ≤ 3.12. The requirement is
  commented in `backend/requirements.txt` so the deviation is not silent.
- **`Psycopg2Instrumentor` refuses to start with `psycopg2-binary`.** The
  instrumentor declares `psycopg2 >= 2.7.3.1` and resolves the *distribution*
  name with `importlib.metadata`; `psycopg2-binary` installs the same module
  under a different distribution name, so the lookup returns `None` and it logs
  `DependencyConflict: requested: "psycopg2 >= 2.7.3.1" but found: "None"`.
  Server spans still appear, but there is **no SELECT/INSERT child span**. The
  fix here is `instrument(..., skip_dep_check=True)` with the reason beside it.
  If you skip that flag, the trace looks fine until you notice the database is
  missing.
- **The exporter is a bridge, not the database.** Scraping postgres_exporter
  proves the exporter can query PostgreSQL; it does not give you per-query
  traces. `pg_up` is the honest liveness signal.
- **The backend's access line has two paths, not one.** It is written to stdout
  (visible in `docker compose logs`) *and* shipped to Loki over OTLP, so it
  appears in Grafana even though Alloy only tails the argus Compose project.
- **`contract_live.sh` derives its target list from `services/*.yaml` and looks
  up a published host port in the argus Compose file.** A descriptor for a
  service that lives in another Compose project therefore makes the script exit
  with `no published host port for demo-postgres` before any check runs. That
  is a platform limitation, not a bug in this example: run `contract_live.sh`
  after `stop.sh`, or with the descriptor removed.

  Measured directly against the exporter (`contract_live.py --service
  demo-postgres=http://localhost:19187`), the scrape-only service scores
  **4/10 PASS, 1 SKIP, 5 FAIL** — not the 10/10 an application descriptor gets:
  no `route`/`status` counter (`route_label`), **507** application series
  against a 500 limit (`series_budget`), the `labelkeep` cardinality guard
  collapsing 29 `pg_*` metric names (`series_integrity`), no traces
  (`otlp_traces`) and no trace-correlated logs (`trace_id_log_format`).
  `log_continuity` is SKIP. This is why the exporter is Tier B enrichment and
  **not** an application that can be run through the whole application
  contract; a database has no request counter and no spans to derive RED from.
- **`platform/attach_proof.sh` refuses to run from a dirty tree.** The example
  files are untracked additions, so run the proof before adding them or with
  `examples/` moved aside; it asserts "no platform file changed", and a
  descriptor installed in `services/` is a platform change.

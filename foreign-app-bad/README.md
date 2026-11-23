# foreign-app-bad

The same orders API, onboarded wrong on purpose. Every deviation is a mistake
a real author makes, and each one maps to a rule in `platform/contract/v1.md`
that must reject it.

| Deviation | Rule |
|---|---|
| only `/health`, no `/healthz` | 3 — health |
| `orders_http_requests_total` has no `service` label | 2 — metrics |
| `route` is the raw path (`/orders/1001`) | 2 — route template |
| plain logs, no `trace_id` | 4 — log correlation |
| Flask instrumented but no `TracerProvider`/OTLP exporter | 1 — no traces |

Runs on 8098. Meant to be onboarded as `services/foreign-app-bad.yaml` and
rejected; do not copy anything here.

```sh
docker build -t foreign-app-bad .
docker run --rm -p 18098:8098 foreign-app-bad
```

> **This app is a test fixture, not a service.** It deliberately violates four contract
> rules (no `/healthz`, raw-path `route` labels, no `trace_id` in logs, no OTLP exporter).
> Phase 3 used it to prove the platform refuses a non-conforming app: `contract_check`
> returned exit 1 with `route_label`, `healthz`, `otlp_traces` and `trace_id_log_format`
> failing. It has no descriptor and no compose entry on purpose — onboarding it would
> make a permanently broken service real.

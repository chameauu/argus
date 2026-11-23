# foreign-app

A small Flask "orders" API written against `platform/contract/v1.md` only,
without looking at `target-app`. It exists to test that onboarding a service
that is *not* shaped like the reference app is still one descriptor.

- `GET /healthz` — liveness, no dependencies
- `GET /orders`, `GET /orders/{id}`, `POST /checkout`
- `GET /metrics` — `orders_http_requests_total{service,route,status}` and
  `orders_http_request_duration_seconds{service,route}`
- `POST /admin/fault?error_rate=&delay_seconds=` — optional, trivial fault knob

Runs on 8099 as uid 10001; gunicorn access log is off. One plain-text access
line per request, ending in `trace_id=<32 lowercase hex>` read from the active
span. OTLP gRPC to `$OTEL_EXPORTER_OTLP_ENDPOINT` (default
`http://otel-collector:4317`), resource `service.name=foreign-app`,
`service.namespace=argus`, `deployment.environment=local`.

```sh
docker build -t foreign-app .
docker run --rm -p 18099:8099 foreign-app
```

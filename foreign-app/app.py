"""foreign-app — a tiny orders API.

Deliberately not the platform's target-app: Flask instead of FastAPI,
its own metric names, its own routes. Contract v1 lives in
platform/contract/v1.md; this file is the whole application.
"""

import logging
import os
import random
import re
import sys
import time

from flask import Flask, Response, abort, g, jsonify, request
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE = "foreign-app"
PORT = int(os.environ.get("PORT", "8099"))

# --- logs -------------------------------------------------------------------
# One line per request, plain text, trace_id last so a derived field can find it.
logger = logging.getLogger("foreign-app.access")
logger.setLevel(logging.INFO)
logger.propagate = False
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_handler)

# Werkzeug's own access log is the Flask equivalent of uvicorn --no-access-log.
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("gunicorn.access").setLevel(logging.WARNING)


def _setup_telemetry(app: Flask) -> None:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
    resource = Resource.create(
        {
            SERVICE_NAME: SERVICE,
            "service.namespace": "argus",
            "deployment.environment": "local",
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    )
    trace.set_tracer_provider(provider)

    # Instrument before registering our hooks so the span is current by the
    # time our before_request handler runs.
    FlaskInstrumentor().instrument_app(app)
    LoggingInstrumentor().instrument()


# --- metrics ----------------------------------------------------------------
# Own names. `service` is set by the platform at scrape time, but setting it
# here too is allowed (the relabel overwrites it with the same value) and keeps
# the series self-describing when scraped ad hoc.
REQUESTS = Counter(
    "orders_http_requests_total",
    "HTTP requests handled by the orders API.",
    ("service", "route", "status"),
)
LATENCY = Histogram(
    "orders_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ("service", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

_PARAM = re.compile(r"<(?:[^:<>]+:)?([^<>]+)>")


def route_template() -> str:
    """Return the route template, never the raw path."""
    rule = request.url_rule
    if rule is None:  # 404 / unmatched: still not a raw path
        return "unmatched"
    return _PARAM.sub(lambda m: "{" + m.group(1) + "}", rule.rule)


def current_trace_id() -> str:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else "0" * 32


# --- app --------------------------------------------------------------------
app = Flask(__name__)
_setup_telemetry(app)

# Minimal in-memory order book; no database, so /healthz stays honest.
ORDERS: dict[int, dict] = {
    1001: {"id": 1001, "sku": "ARGUS-TEE", "qty": 1, "total_cents": 2400, "status": "paid"},
    1002: {"id": 1002, "sku": "ARGUS-MUG", "qty": 2, "total_cents": 3600, "status": "paid"},
}
_next_id = 2000

FAULT = {"error_rate": 0.0, "delay_seconds": 0.0}


@app.before_request
def _start_timer() -> None:
    g.started = time.perf_counter()
    if request.path in ("/healthz", "/metrics", "/admin/fault"):
        return
    if FAULT["delay_seconds"]:
        time.sleep(FAULT["delay_seconds"])
    if FAULT["error_rate"] and random.random() < FAULT["error_rate"]:
        abort(503, description="injected fault")


@app.after_request
def _record(resp: Response) -> Response:
    route = route_template()
    duration = time.perf_counter() - getattr(g, "started", time.perf_counter())
    status = str(resp.status_code)
    REQUESTS.labels(service=SERVICE, route=route, status=status).inc()
    LATENCY.labels(service=SERVICE, route=route).observe(duration)
    logger.info(
        "access method=%s route=%s status=%s duration_ms=%.2f trace_id=%s",
        request.method,
        route,
        status,
        duration * 1000.0,
        current_trace_id(),
    )
    return resp


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})


@app.get("/orders")
def list_orders():
    return jsonify(orders=list(ORDERS.values()), count=len(ORDERS))


@app.get("/orders/<int:id>")
def get_order(id: int):
    order = ORDERS.get(id)
    if order is None:
        return jsonify(error="order not found", id=id), 404
    return jsonify(order)


@app.post("/checkout")
def checkout():
    global _next_id
    payload = request.get_json(silent=True) or {}
    sku = payload.get("sku")
    qty = payload.get("qty", 1)
    if not isinstance(sku, str) or not sku or not isinstance(qty, int) or qty < 1:
        return jsonify(error="sku (string) and qty (int >= 1) are required"), 400
    order = {
        "id": _next_id,
        "sku": sku,
        "qty": qty,
        "total_cents": int(payload.get("total_cents", 1200 * qty)),
        "status": "paid",
    }
    ORDERS[_next_id] = order
    _next_id += 1
    return jsonify(order), 201


@app.post("/admin/fault")
def set_fault():
    """Trivial fault control so onboarding can exercise error/latency rules."""
    for key in ("error_rate", "delay_seconds"):
        raw = request.args.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except ValueError:
            return jsonify(error=f"{key} must be a number"), 400
        if key == "error_rate":
            value = min(max(value, 0.0), 1.0)
        else:
            value = min(max(value, 0.0), 30.0)
        FAULT[key] = value
    return jsonify(FAULT)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)

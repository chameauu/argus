"""foreign-app-bad — the same orders API, onboarded wrong on purpose.

This service is what a plausible first attempt looks like when the author
never reads platform/contract/v1.md. Each deviation is marked WRONG.
It exists so the onboarding check can prove it rejects a service.
"""

import logging
import os
import random
import sys
import time

from flask import Flask, Response, abort, g, jsonify, request
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

PORT = int(os.environ.get("PORT", "8098"))

# Plain text logs with no trace_id. WRONG: rule 4 needs trace_id=<32 hex>.
logger = logging.getLogger("foreign-app-bad")
logger.setLevel(logging.INFO)
logger.propagate = False
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_handler)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("gunicorn.access").setLevel(logging.WARNING)

app = Flask(__name__)

# WRONG: instrumented, but no TracerProvider and no OTLP exporter are wired,
# so spans are non-recording and never reach the collector (rule 1).
FlaskInstrumentor().instrument_app(app)

# WRONG: no `service` label (rule 2 / platform relabel can add it, but this
# author did not know that and did not declare it).
REQUESTS = Counter(
    "orders_http_requests_total",
    "HTTP requests handled by the orders API.",
    ("route", "status"),
)
LATENCY = Histogram(
    "orders_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ("route",),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

ORDERS: dict[int, dict] = {
    1001: {"id": 1001, "sku": "ARGUS-TEE", "qty": 1, "total_cents": 2400, "status": "paid"},
    1002: {"id": 1002, "sku": "ARGUS-MUG", "qty": 2, "total_cents": 3600, "status": "paid"},
}
_next_id = 2000
FAULT = {"error_rate": 0.0, "delay_seconds": 0.0}


# No /healthz. WRONG: rule 3 checks /healthz; /health is a different URL.
@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})


@app.before_request
def _start_timer() -> None:
    g.started = time.perf_counter()
    if request.path in ("/health", "/metrics", "/admin/fault"):
        return
    if FAULT["delay_seconds"]:
        time.sleep(FAULT["delay_seconds"])
    if FAULT["error_rate"] and random.random() < FAULT["error_rate"]:
        abort(503, description="injected fault")


@app.after_request
def _record(resp: Response) -> Response:
    # WRONG: the raw path, not a template. /orders/1, /orders/2, ... each get
    # their own series (rule 2: route must be a template).
    route = request.path
    status = str(resp.status_code)
    duration = time.perf_counter() - getattr(g, "started", time.perf_counter())
    REQUESTS.labels(route=route, status=status).inc()
    LATENCY.labels(route=route).observe(duration)
    # WRONG: no trace_id, because there is no span to read it from.
    logger.info(
        "access method=%s route=%s status=%s duration_ms=%.2f",
        request.method,
        route,
        status,
        duration * 1000.0,
    )
    return resp


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
    for key in ("error_rate", "delay_seconds"):
        raw = request.args.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except ValueError:
            return jsonify(error=f"{key} must be a number"), 400
        FAULT[key] = min(max(value, 0.0), 1.0 if key == "error_rate" else 30.0)
    return jsonify(FAULT)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)

import logging
import os
import random
import threading
import time
from collections import deque

from fastapi import FastAPI, Query, Request, Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest


def configure_tracing() -> None:
    resource = Resource.create(
        {
            "service.name": os.getenv("OTEL_SERVICE_NAME", "custom-exporter"),
            "service.namespace": os.getenv("OTEL_SERVICE_NAMESPACE", "argus"),
            "deployment.environment": os.getenv("DEPLOYMENT_ENVIRONMENT", "local"),
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317"),
                insecure=True,
            )
        )
    )
    trace.set_tracer_provider(provider)


configure_tracing()

LoggingInstrumentor().instrument(set_logging_format=True)
logger = logging.getLogger("custom-exporter")
logger.setLevel(logging.INFO)

app = FastAPI(title="custom-exporter")

overrides: dict[str, float | None] = {
    "job_queue_depth": None,
    "webhook_delivery_success_ratio": None,
    "webhook_delivery_seconds": None,
}

active_users_total = Gauge("active_users_total", "Current active users")
job_queue_depth = Gauge("job_queue_depth", "Current queue depth")
webhook_delivery_success_ratio = Gauge(
    "webhook_delivery_success_ratio",
    "Rolling ratio of successful webhook deliveries",
)
jobs_queued_total = Counter("jobs_queued_total", "Total jobs queued")
jobs_processed_total = Counter(
    "jobs_processed_total",
    "Total jobs processed",
    labelnames=("status",),
)
webhook_delivery_duration_seconds = Histogram(
    "webhook_delivery_duration_seconds",
    "Webhook delivery duration",
    ["route"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)

# Contract v1 rule 2: request counter + latency histogram with a templated
# route label. Purely additive — the business metrics above and the /admin/*
# semantics are unchanged.
http_requests_total = Counter(
    "custom_exporter_http_requests_total",
    "Total HTTP requests handled by custom-exporter",
    ["route", "status"],
)
http_request_duration_seconds = Histogram(
    "custom_exporter_http_request_duration_seconds",
    "HTTP request latency for custom-exporter routes in seconds",
    ["route"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)


QUEUE_TARGET = 50.0
QUEUE_MIN = 30.0
QUEUE_MAX = 70.0
QUEUE_REVERSION = 0.25
SUCCESS_RATIO_WINDOW = 200
# Route label for the outbound delivery histogram. The descriptor allowlists this
# value so the generated latency SLO measures deliveries, not scrape traffic.
DELIVERY_ROUTE = "/webhook-delivery"


def _simulate_business_metrics():
    # Bounded, mean-reverting walk. Arrivals and drains share the same mean and
    # a pull toward QUEUE_TARGET absorbs the difference, so the gauge stays in
    # [QUEUE_MIN, QUEUE_MAX] by itself: never negative, never near the 100
    # alert threshold. POST /admin/override still wins over the simulated value
    # and can force any value (including >100) for alert demonstrations.
    queue_depth = QUEUE_TARGET
    recent_deliveries: deque[bool] = deque(maxlen=SUCCESS_RATIO_WINDOW)

    while True:
        active_users_total.set(random.randint(50, 500))

        arrivals = random.randint(0, 8)
        drained = random.randint(0, 8)
        queue_depth += arrivals - drained + (QUEUE_TARGET - queue_depth) * QUEUE_REVERSION
        queue_depth = min(QUEUE_MAX, max(QUEUE_MIN, queue_depth))
        override_depth = overrides["job_queue_depth"]
        if override_depth is not None:
            job_queue_depth.set(override_depth)
        else:
            job_queue_depth.set(queue_depth)

        for _ in range(arrivals):
            jobs_queued_total.inc()

        for _ in range(drained):
            failed = random.random() < 0.05
            status = "failure" if failed else "success"
            jobs_processed_total.labels(status=status).inc()

        # Rolling window instead of a lifetime average: the ratio stays in a
        # tight healthy band (~0.83) and reacts to a real delivery regression.
        # Healthy by default: every delivery finishes in 20-300 ms, so both the
        # latency SLO and the per-route p99 RED rule stay quiet. An earlier 0.5%
        # slow tail was removed because one slow delivery per five minutes is
        # enough to push p99 over 0.5 s at this volume. A forced value via
        # POST /admin/override?webhook_delivery_seconds=N makes every delivery
        # take N seconds, which is the demonstrated way to fire both alerts.
        forced_seconds = overrides["webhook_delivery_seconds"]
        for _ in range(random.randint(1, 5)):
            if forced_seconds is not None:
                duration = forced_seconds
            else:
                duration = random.uniform(0.02, 0.30)
            webhook_delivery_duration_seconds.labels(route=DELIVERY_ROUTE).observe(duration)
            recent_deliveries.append(duration < 2.5)

        override_ratio = overrides["webhook_delivery_success_ratio"]
        if override_ratio is not None:
            webhook_delivery_success_ratio.set(override_ratio)
        elif recent_deliveries:
            webhook_delivery_success_ratio.set(sum(recent_deliveries) / len(recent_deliveries))

        time.sleep(5)


@app.on_event("startup")
def start_background_thread():
    thread = threading.Thread(target=_simulate_business_metrics, daemon=True)
    thread.start()


@app.middleware("http")
async def access_log(request: Request, call_next):
    """One access line per request carrying the active trace id.

    Registered before FastAPIInstrumentor.instrument_app() so the OTel server
    span wraps this middleware and LoggingInstrumentor can render
    trace_id=<32 lowercase hex>. The simulator thread never logs, and an
    invalid/absent span would simply render an all-zero trace id rather than
    raising.
    """
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        template = getattr(route, "path", None) or request.url.path
        duration = time.perf_counter() - started
        logger.info(
            "%s %s %d %.1fms",
            request.method,
            template,
            status,
            duration * 1000.0,
        )
        metric_route = getattr(route, "path", None) or "unmatched"
        http_request_duration_seconds.labels(route=metric_route).observe(duration)
        http_requests_total.labels(route=metric_route, status=str(status)).inc()


FastAPIInstrumentor.instrument_app(app)


@app.post("/admin/override")
def set_override(
    queue_depth: float | None = Query(default=None, alias="job_queue_depth"),
    success_ratio: float | None = Query(default=None, alias="webhook_delivery_success_ratio"),
    delivery_seconds: float | None = Query(default=None, alias="webhook_delivery_seconds"),
) -> dict[str, float | None]:
    if queue_depth is not None:
        overrides["job_queue_depth"] = queue_depth
    if success_ratio is not None:
        overrides["webhook_delivery_success_ratio"] = success_ratio
    if delivery_seconds is not None:
        overrides["webhook_delivery_seconds"] = delivery_seconds
    return overrides


@app.post("/admin/reset")
def reset_overrides() -> dict[str, float | None]:
    for key in overrides:
        overrides[key] = None
    return overrides


@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok"}

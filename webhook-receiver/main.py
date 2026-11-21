import json
import logging
import os
import time
from collections import deque

from fastapi import FastAPI, Request
from fastapi.responses import Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest


def configure_tracing() -> None:
    resource = Resource.create(
        {
            "service.name": os.getenv("OTEL_SERVICE_NAME", "webhook-receiver"),
            "service.namespace": os.getenv("OTEL_SERVICE_NAMESPACE", "argus"),
            "deployment.environment": os.getenv("DEPLOYMENT_ENVIRONMENT", "local"),
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=os.getenv(
                    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317"
                ),
                insecure=True,
            )
        )
    )
    trace.set_tracer_provider(provider)


configure_tracing()

LoggingInstrumentor().instrument(set_logging_format=True)
logger = logging.getLogger("webhook-receiver")
logger.setLevel(logging.INFO)

app = FastAPI(title="webhook-receiver")

webhook_received_total = Counter(
    "webhook_received_total",
    "Total alert webhooks received",
    ["route", "status"],
)

webhook_received_duration_seconds = Histogram(
    "webhook_received_duration_seconds",
    "Webhook receipt latency in seconds",
    ["route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)

received_payloads = deque(maxlen=100)


@app.middleware("http")
async def access_log(request: Request, call_next):
    """One access line per request carrying the active trace id.

    Registered before FastAPIInstrumentor.instrument_app() so the OTel server
    span wraps this middleware and LoggingInstrumentor can render
    trace_id=<32 lowercase hex>.
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
        logger.info(
            "%s %s %d %.1fms",
            request.method,
            template,
            status,
            (time.perf_counter() - started) * 1000.0,
        )


FastAPIInstrumentor.instrument_app(app)


@app.post("/critical")
async def critical(request: Request):
    started = time.perf_counter()
    payload = await request.json()
    logger.info("CRITICAL alert received: %s", json.dumps(payload)[:500])
    received_payloads.appendleft({"route": "critical", "payload": payload})
    webhook_received_duration_seconds.labels(route="critical").observe(
        time.perf_counter() - started
    )
    webhook_received_total.labels(route="critical", status="accepted").inc()
    return {"status": "received"}


@app.post("/warning")
async def warning(request: Request):
    started = time.perf_counter()
    payload = await request.json()
    logger.info("WARNING alert received: %s", json.dumps(payload)[:500])
    received_payloads.appendleft({"route": "warning", "payload": payload})
    webhook_received_duration_seconds.labels(route="warning").observe(
        time.perf_counter() - started
    )
    webhook_received_total.labels(route="warning", status="accepted").inc()
    return {"status": "received"}


@app.get("/received")
def received():
    return list(received_payloads)


@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok"}

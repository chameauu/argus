import os
import random
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest


def configure_tracing() -> None:
    resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "target-app")})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


configure_tracing()

app = FastAPI(title="target-app")
FastAPIInstrumentor.instrument_app(app)

http_requests_total = Counter(
    "target_app_http_requests_total",
    "Total HTTP requests handled by target-app",
    ["endpoint", "status"],
)

http_request_duration_seconds = Histogram(
    "target_app_http_request_duration_seconds",
    "HTTP request latency for target-app endpoints",
    ["endpoint"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)


@app.get("/health")
def health():
    http_requests_total.labels(endpoint="/health", status="200").inc()
    return {"status": "ok"}


@app.get("/work")
def work():
    start = time.time()
    time.sleep(random.uniform(0.01, 0.08))
    http_request_duration_seconds.labels(endpoint="/work").observe(time.time() - start)
    http_requests_total.labels(endpoint="/work", status="200").inc()
    return {"result": "done"}


@app.get("/error-prone")
def error_prone():
    start = time.time()
    if random.random() < 0.15:
        http_request_duration_seconds.labels(endpoint="/error-prone").observe(time.time() - start)
        http_requests_total.labels(endpoint="/error-prone", status="500").inc()
        raise HTTPException(status_code=500, detail="simulated failure")
    http_request_duration_seconds.labels(endpoint="/error-prone").observe(time.time() - start)
    http_requests_total.labels(endpoint="/error-prone", status="200").inc()
    return {"result": "ok"}


@app.get("/slow")
def slow():
    start = time.time()
    if random.random() < 0.10:
        delay = random.uniform(0.6, 1.2)
        time.sleep(delay)
    else:
        time.sleep(random.uniform(0.02, 0.1))
    http_request_duration_seconds.labels(endpoint="/slow").observe(time.time() - start)
    http_requests_total.labels(endpoint="/slow", status="200").inc()
    return {"result": "ok"}


@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

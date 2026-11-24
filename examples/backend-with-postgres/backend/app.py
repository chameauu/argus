"""demo-backend — a FastAPI + psycopg2 app that attaches to Argus with env vars only.

Tier A: the only platform-facing configuration is ``OTEL_SERVICE_NAME`` and
``OTEL_EXPORTER_OTLP_ENDPOINT``. No descriptor is written for this app; the
collector stamps ``service`` from the OTLP resource and the spanmetrics
connector derives RED from the FastAPI server spans.

The database is *not* instrumented with an OTel SDK — PostgreSQL cannot emit
OTLP. The psycopg2 client library is instrumented here, which is what gives a
trace a client child span with ``db.system=postgresql``. The database itself is
integrated separately, by scraping postgres_exporter (see the example README).
"""

import logging
import os
import time
from contextlib import asynccontextmanager

import psycopg2
from fastapi import FastAPI, HTTPException, Request
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pydantic import BaseModel

SERVICE = os.environ.get("OTEL_SERVICE_NAME", "demo-backend")
ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318").rstrip("/")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:pw@postgres:5432/orders")

resource = Resource.create(
    {
        "service.name": SERVICE,
        "service.namespace": "argus",
        "deployment.environment": "local",
    }
)

# --- traces -----------------------------------------------------------------
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{ENDPOINT}/v1/traces"))
)
trace.set_tracer_provider(tracer_provider)

# --- metrics ----------------------------------------------------------------
metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=f"{ENDPOINT}/v1/metrics"),
    export_interval_millis=3000,
)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("demo-backend")
REQUESTS = meter.create_counter(
    "demo_backend_http_requests_total",
    description="Requests served by demo-backend, labelled by route template and status.",
)
LATENCY = meter.create_histogram(
    "demo_backend_http_request_duration_seconds",
    unit="s",
    description="Request duration in seconds.",
)

# --- logs -------------------------------------------------------------------
logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{ENDPOINT}/v1/logs"))
)
set_logger_provider(logger_provider)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger().addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
LoggingInstrumentor().instrument(set_logging_format=False)
ACCESS = logging.getLogger("demo-backend.access")

# psycopg2 must be patched *before* any connection is opened. The module is
# always used as ``psycopg2.connect(...)`` below so the patched attribute is the
# one called (a ``from psycopg2 import connect`` captured earlier would be the
# original function and would produce no DB spans).
#
# skip_dep_check: opentelemetry-instrumentation-psycopg2 0.50b0 declares its
# dependency as the distribution ``psycopg2 >= 2.7.3.1`` and resolves it with
# importlib.metadata. ``psycopg2-binary`` installs the same module under the
# distribution name ``psycopg2-binary``, so the lookup returns None and the
# instrumentor refuses to run ("requested: psycopg2 ... but found: None"). The
# code is present and compatible; the check is a false negative. Without this
# flag the server spans exist but no SELECT/INSERT child span is emitted.
Psycopg2Instrumentor().instrument(tracer_provider=tracer_provider, skip_dep_check=True)


def connect():
    return psycopg2.connect(DATABASE_URL, connect_timeout=3)


def init_db():
    """Create and seed the table; retry so a slow postgres cannot crash startup."""
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS orders (
                        id serial primary key,
                        item text,
                        created_at timestamptz default now()
                    )
                    """
                )
                cur.execute("SELECT count(*) FROM orders")
                if cur.fetchone()[0] == 0:
                    cur.executemany(
                        "INSERT INTO orders (item) VALUES (%s)",
                        [("widget",), ("gizmo",), ("sprocket",)],
                    )
            return
        except psycopg2.OperationalError as exc:  # database not accepting yet
            last_error = exc
            time.sleep(1)
    raise RuntimeError(f"database did not become ready: {last_error}")


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="demo-backend", lifespan=lifespan)


# Added *before* FastAPIInstrumentor so the OTel middleware is inserted on top
# and the request is already inside its server span when this handler runs; that
# is what makes the logged trace_id the trace's id, not 32 zeroes.
@app.middleware("http")
async def access_log(request: Request, call_next):
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = getattr(request.scope.get("route"), "path", "unmatched")
        context = trace.get_current_span().get_span_context()
        trace_id = format(context.trace_id, "032x")
        duration = time.perf_counter() - started
        REQUESTS.add(1, {"route": route, "status": str(status)})
        LATENCY.record(duration, {"route": route, "status": str(status)})
        ACCESS.info(
            "access method=%s route=%s status=%s duration_ms=%.1f trace_id=%s",
            request.method,
            route,
            status,
            duration * 1000,
            trace_id,
        )


@app.get("/health")
def health():
    """Liveness only: deliberately does not touch the database."""
    return {"status": "ok", "service": SERVICE}


@app.get("/orders")
def list_orders():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, item, created_at FROM orders ORDER BY id")
        rows = cur.fetchall()
    return [{"id": row[0], "item": row[1], "created_at": row[2].isoformat()} for row in rows]


@app.get("/orders/{order_id}")
def get_order(order_id: int):
    with connect() as conn, conn.cursor() as cur:
        # Bound parameter, not string interpolation: the span still records the
        # statement shape and the instrumentation does not need literal values.
        cur.execute("SELECT id, item, created_at FROM orders WHERE id = %s", (order_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="order not found")
    return {"id": row[0], "item": row[1], "created_at": row[2].isoformat()}


class OrderIn(BaseModel):
    item: str


@app.post("/orders", status_code=201)
def create_order(order: OrderIn):
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO orders (item) VALUES (%s) RETURNING id, item, created_at",
            (order.item,),
        )
        row = cur.fetchone()
    return {"id": row[0], "item": row[1], "created_at": row[2].isoformat()}


# Instrument last: the OTel middleware becomes the outermost one, so the access
# middleware above runs inside a valid server span.
FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)

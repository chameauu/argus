"""attach-demo — the throwaway app behind platform/attach_proof.sh.

It is deliberately plain: stdlib ``http.server``, no framework, and only the
OpenTelemetry SDK. The platform learns who it is from ``OTEL_SERVICE_NAME`` and
where to send signals from ``OTEL_EXPORTER_OTLP_ENDPOINT``; it is given nothing
else and nothing in this repository is changed to make the proof pass.

The counter is named ``attached_requests_total`` on purpose. It must *not*
match the ``*_http_requests_total`` convention the dashboard's span-derived
path is meant to replace, so a green RED row proves the spans were enough.
"""

import json
import logging
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

PORT = 8080
ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318").rstrip("/")
SERVICE = os.environ.get("OTEL_SERVICE_NAME", "attach-demo")
ORDER_PATH = re.compile(r"^/orders/([^/]+)$")

resource = Resource.create({SERVICE_NAME: SERVICE})

tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{ENDPOINT}/v1/traces"))
)
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("attach-demo")

metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=f"{ENDPOINT}/v1/metrics"),
    export_interval_millis=3000,
)
metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))
REQUESTS = metrics.get_meter("attach-demo").create_counter(
    "attached_requests_total",
    description="Requests seen by attach-demo; intentionally not *_http_requests_total.",
)

logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{ENDPOINT}/v1/logs"))
)
set_logger_provider(logger_provider)

log = logging.getLogger("attach-demo")
log.setLevel(logging.INFO)
log.propagate = False
log.addHandler(LoggingHandler(logger_provider=logger_provider))


class Handler(BaseHTTPRequestHandler):
    """One line per request with a 32-hex trace_id, plus a server span."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        order_id = None
        if path == "/health":
            route, status, body = "/health", 200, {"status": "ok"}
        elif path == "/":
            route, status, body = "/", 200, {"service": SERVICE, "route": "/"}
        else:
            match = ORDER_PATH.match(path)
            if match:
                order_id = match.group(1)
                route, status = "/orders/{id}", 200
                body = {"order_id": order_id, "route": route}
            else:
                route, status, body = "unmatched", 404, {"error": "not found"}

        with tracer.start_as_current_span(f"GET {route}", kind=trace.SpanKind.SERVER) as span:
            span.set_attribute("http.request.method", "GET")
            span.set_attribute("http.route", route)
            span.set_attribute("http.status_code", status)
            span.set_attribute("url.path", path)
            trace_id = format(span.get_span_context().trace_id, "032x")
            REQUESTS.add(1, {"route": route, "status": str(status)})
            body["trace_id"] = trace_id
            log.info("access method=GET route=%s status=%s trace_id=%s", route, status, trace_id)

            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def log_message(self, *args):
        """Silence the stdlib access log; our own line goes through OTLP."""


if __name__ == "__main__":
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)

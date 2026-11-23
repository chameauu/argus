#!/usr/bin/env python3
"""Live conformance checks for the argus platform contract v1.

Ten checks per service, each PASS / FAIL / SKIP with quoted evidence:
  metrics_reachable, service_label, route_label, series_budget, series_integrity,
  healthz, otlp_traces, trace_id_log_format, log_continuity, descriptor

Three platform-level checks run before the per-service table:
  otlp_metrics — push one OTLP/JSON gauge to the collector and require
  Prometheus to store it. Scrape-based checks cannot see this: an OTLP push
  can be rejected or dropped at the collector while every scrape stays green.
  otlp_service_name — push the same gauge without a resource service.name and
  require Prometheus to store nothing. The collector drops unattributed
  datapoints but still answers 200, so only the store proves the drop.
  span_metrics — require span-derived RED to exist. It is the only curation
  source that needs no metric-name convention, and it vanished once when the
  connector lived in a running container instead of the committed config.

`config_parity` is not in this tool: it compares the running containers with
the committed config and lives in platform/config_parity.py, which
platform/contract_live.sh runs before this one.

Descriptor parsing and validation are imported from descriptor.py, the same
module the offline lint uses. This tool needs the demo stack: Prometheus,
Tempo, Loki and the collector.

Usage:
  python3 platform/contract_live.py \
      --service target-app=http://localhost:18000 \
      --service custom-exporter=http://localhost:19200 \
      --service webhook-receiver=http://localhost:18090 [--json] [--timeout 5] \
      [--prometheus http://localhost:19090] [--collector http://localhost:4318] \
      [--services-dir services] [--max-log-gap 60]

Exit code: 0 when every check is PASS or SKIP, 1 when any check FAILs, 2 on a
usage error. Standard library only.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass

from descriptor import (
    INFRASTRUCTURE_ROUTES,
    ROUTE_LABELS,
    is_infrastructure_route,
    looks_like_raw_id,
    normalise_route,
    read_descriptor,
    validate_descriptor,
)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

TEMPO_URL = os.environ.get("ARGUS_TEMPO_URL", "http://localhost:13200").rstrip("/")
LOKI_URL = os.environ.get("ARGUS_LOKI_URL", "http://localhost:13100").rstrip("/")
DEFAULT_PROMETHEUS_URL = os.environ.get("ARGUS_PROMETHEUS_URL", "http://localhost:19090")
DEFAULT_COLLECTOR_URL = os.environ.get("ARGUS_COLLECTOR_URL", "http://localhost:4318")
OTLP_PROBE_METRIC = "argus_contract_probe"
OTLP_PROBE_WINDOW_S = 20.0
OTLP_SERVICE_NAME_WINDOW_S = 10.0
OTLP_PROBE_INTERVAL_S = 1.0

# Runtime/client-library families are exempt from the series budget.
EXEMPT_PREFIXES = ("go_", "python_", "process_", "promhttp_")
# Prometheus synthesises these per target; metric_relabel_configs cannot label them.
SYNTHETIC_SERIES = "up|scrape_.*"
HEALTH_BUDGET_S = 1.0
SERIES_LIMIT = 500
ROUTE_VALUE_LIMIT = 50
TRACE_LOOKBACK_S = 15 * 60
LOG_LOOKBACK_NS = 15 * 60 * 1_000_000_000
TRACE_ID_RE = re.compile(r"trace_id=[0-9a-f]{32}")
LOG_CONTINUITY_WINDOW_S = 10 * 60
LOG_CONTINUITY_MAX_AGE_S = 120.0
LOG_CONTINUITY_MIN_LINES = 5
DEFAULT_MAX_LOG_GAP_S = 60.0
SAMPLE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})?\s+(\S+)(?:\s+\S+)?(?:\s+#.*)?$")
LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
RAW_ID_SEGMENT_RE = re.compile(r"[0-9a-fA-F]{32}")
INT_RE = re.compile(r"[-+]?\d+")
FLOAT_RE = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")


@dataclass
class Result:
    check: str
    verdict: str
    evidence: str


def fetch(url, timeout):
    """GET a URL. Return (status, body_text, elapsed_s, error_string)."""
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": "argus-contract-check/v1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, body, time.monotonic() - started, None
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return exc.code, body, time.monotonic() - started, None
    except Exception as exc:
        return None, "", time.monotonic() - started, describe_error(exc, timeout)


def describe_error(exc, timeout):
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
        return f"timeout after {timeout:g}s"
    return f"{type(reason).__name__}: {reason}"


def post_otlp_metrics(collector, body, timeout):
    """POST one OTLP/JSON payload. Return (status, body_text, elapsed_s, error)."""
    started = time.monotonic()
    request = urllib.request.Request(
        f"{collector}/v1/metrics",
        data=body.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "argus-contract-check/v1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
            return response.status, text, time.monotonic() - started, None
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", "replace")
        except Exception:
            text = ""
        return exc.code, text, time.monotonic() - started, None
    except Exception as exc:
        return None, "", time.monotonic() - started, describe_error(exc, timeout)


def otlp_probe_body(probe, service_name="argus-contract-probe"):
    """One gauge datapoint whose `probe` label makes the series unambiguous.

    ``service_name=None`` omits the resource attribute entirely: that is the
    unattributed-resource case the collector is required to drop.
    """
    attributes = []
    if service_name is not None:
        attributes.append({"key": "service.name", "value": {"stringValue": service_name}})
    payload = {
        "resourceMetrics": [
            {
                "resource": {"attributes": attributes},
                "scopeMetrics": [
                    {
                        "scope": {},
                        "metrics": [
                            {
                                # No `unit`: the OTLP→Prometheus translation
                                # appends the UCUM suffix, so unit "1" renders
                                # the series as "..._ratio" and an exact-name
                                # query would miss data that did land.
                                "name": OTLP_PROBE_METRIC,
                                "gauge": {
                                    "dataPoints": [
                                        {
                                            "attributes": [
                                                {
                                                    "key": "probe",
                                                    "value": {"stringValue": probe},
                                                }
                                            ],
                                            "timeUnixNano": str(time.time_ns()),
                                            "asInt": "1",
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }
    return json.dumps(payload)


def parse_exposition(text):
    """Parse Prometheus text exposition into [(name, labels_dict)]."""
    samples = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = SAMPLE_RE.match(line)
        if not match:
            raise ValueError(f"malformed line {lineno}: {line[:70]!r}")
        name, _, label_blob, value = match.groups()
        try:
            float(value)
        except ValueError as err:
            raise ValueError(f"malformed value on line {lineno}: {line[:70]!r}") from err
        samples.append((name, dict(LABEL_RE.findall(label_blob or ""))))
    if not samples:
        raise ValueError("no samples found")
    return samples


def check_metrics_reachable(base, timeout):
    """Return (Result, parsed samples or None)."""
    status, body, elapsed, error = fetch(f"{base}/metrics", timeout)
    if error:
        return Result("metrics_reachable", FAIL, f"GET /metrics failed: {error}"), None
    if status != 200:
        detail = f"GET /metrics -> HTTP {status} in {elapsed:.3f}s"
        return Result("metrics_reachable", FAIL, detail), None
    try:
        samples = parse_exposition(body)
    except ValueError as exc:
        detail = f"GET /metrics -> 200 but exposition unparseable: {exc}"
        return Result("metrics_reachable", FAIL, detail), None

    series = {(name, tuple(sorted(labels.items()))) for name, labels in samples}
    evidence = f"GET /metrics -> 200, {len(samples)} samples / {len(series)} distinct series"
    evidence += f" in {elapsed:.3f}s"
    if len(series) > SERIES_LIMIT:
        evidence += f" (over contract limit of {SERIES_LIMIT} series)"
    return Result("metrics_reachable", PASS, evidence), samples


def prometheus_count(prometheus, query, timeout):
    """Run an instant count query. Return (float or None, error_string or None)."""
    params = urllib.parse.urlencode({"query": query})
    status, body, _, error = fetch(f"{prometheus}/api/v1/query?{params}", timeout)
    if error:
        return None, f"prometheus unreachable: {error}"
    if status != 200:
        return None, f"prometheus query -> HTTP {status}: {body[:100].strip()}"
    try:
        payload = json.loads(body)
        if payload.get("status") != "success":
            return None, f"prometheus error: {payload.get('error', 'unknown')}"
        result = payload["data"]["result"]
        return sum(float(sample["value"][1]) for sample in result), None
    except (ValueError, KeyError, TypeError) as exc:
        return None, f"prometheus response unreadable: {type(exc).__name__}"


def check_otlp_metrics(collector, prometheus, timeout):
    """Push one OTLP gauge; PASS only if Prometheus stores the series.

    This is platform-level, not per-service: the collector's OTLP receiver and
    its metrics pipeline are shared, so a metric dropped here is lost to every
    service while each scrape still reports green.
    """
    probe = uuid.uuid4().hex[:8]
    post_started = time.monotonic()
    status, body, elapsed, error = post_otlp_metrics(collector, otlp_probe_body(probe), timeout)
    if error:
        detail = f"OTLP/HTTP POST {collector}/v1/metrics failed: {error}"
        verdict = SKIP if "refused" in error.lower() else FAIL
        return Result("otlp_metrics", verdict, detail)
    if status != 200:
        detail = f"OTLP/HTTP POST {collector}/v1/metrics -> HTTP {status} in {elapsed:.3f}s"
        snippet = body[:100].strip()
        if snippet:
            detail += f": {snippet}"
        return Result("otlp_metrics", FAIL, detail)

    # Tolerate a unit suffix: the exporter is free to render `foo` as
    # `foo_ratio`/`foo_total`. The `probe` label is what proves this run landed.
    query = f'count({{__name__=~"{OTLP_PROBE_METRIC}.*",probe="{probe}"}})'
    deadline = time.monotonic() + OTLP_PROBE_WINDOW_S
    while time.monotonic() < deadline:
        stored, query_error = prometheus_count(prometheus, query, min(timeout, 2.0))
        if query_error:
            return Result("otlp_metrics", FAIL, query_error)
        if stored and stored >= 1:
            waited = time.monotonic() - post_started
            evidence = (
                f'OTLP/HTTP 200, Prometheus stored {OTLP_PROBE_METRIC}{{probe="{probe}"}} '
                f"({stored:g} series) after {waited:.1f}s"
            )
            return Result("otlp_metrics", PASS, evidence)
        time.sleep(OTLP_PROBE_INTERVAL_S)
    detail = (
        f"OTLP/HTTP accepted (200) but no series in Prometheus after "
        f"{OTLP_PROBE_WINDOW_S:g}s — the collector accepted a metric it will never store"
    )
    return Result("otlp_metrics", FAIL, detail)


def check_span_metrics(collector, prometheus, timeout):
    """Span-derived RED must exist, because it is the only source that needs no naming convention.

    This is the gate for the failure that motivated it: the spanmetrics connector
    lived in a running container while being absent from the committed config, so a
    clean `docker compose up` silently removed the tier. In CI the stack boots from
    the committed files, so a missing connector makes this check fail rather than
    leaving a silent gap. An empty store still fails: with the load generator
    running there is always traffic.
    """
    query = "count by (service)(spanmetrics_calls_total)"
    series = prometheus_series(prometheus, query, timeout)
    if not series:
        return Result(
            "span_metrics",
            FAIL,
            "no spanmetrics_calls_total series: the collector is not deriving RED from spans "
            "(is the spanmetrics connector in the committed config and the container restarted?)",
        )
    services = sorted(item.get("metric", {}).get("service", "?") for item in series)
    return Result(
        "span_metrics",
        PASS,
        f"span-derived RED present for {len(services)} service(s): {', '.join(services)}",
    )


def prometheus_series(prometheus, query, timeout):
    """Run an instant query; return the raw result series (metric dicts)."""
    params = urllib.parse.urlencode({"query": query})
    status, body, _, error = fetch(f"{prometheus}/api/v1/query?{params}", timeout)
    if error or status != 200:
        return []
    try:
        return json.loads(body)["data"]["result"]
    except (ValueError, KeyError, TypeError):
        return []


def check_otlp_service_name(collector, prometheus, timeout):
    """An unattributed OTLP datapoint must not be stored.

    The collector silently drops datapoints whose resource has no service.name
    and still answers 200/partialSuccess, so the HTTP response proves nothing.
    A thrown-away datapoint is the *desired* outcome here: this check PASSes
    when the store stays empty and FAILs when the anonymous series appears
    (that is how a "conforming" app becomes unattributable).
    """
    probe = uuid.uuid4().hex[:8]
    post_started = time.monotonic()
    body = otlp_probe_body(probe, service_name=None)
    status, text, elapsed, error = post_otlp_metrics(collector, body, timeout)
    if error:
        detail = f"OTLP/HTTP POST {collector}/v1/metrics failed: {error}"
        verdict = SKIP if "refused" in error.lower() else FAIL
        return Result("otlp_service_name", verdict, detail)
    if status != 200:
        detail = f"OTLP/HTTP POST {collector}/v1/metrics -> HTTP {status} in {elapsed:.3f}s"
        snippet = text[:100].strip()
        if snippet:
            detail += f": {snippet}"
        return Result("otlp_service_name", FAIL, detail)

    selector = f'{{__name__=~"{OTLP_PROBE_METRIC}.*",probe="{probe}"}}'
    deadline = time.monotonic() + OTLP_SERVICE_NAME_WINDOW_S
    while True:
        stored, query_error = prometheus_count(prometheus, f"count({selector})", min(timeout, 2.0))
        if query_error:
            return Result("otlp_service_name", FAIL, query_error)
        if stored and stored >= 1:
            landed = prometheus_series(prometheus, selector, min(timeout, 2.0))
            labels = landed[0].get("metric", {}) if landed else {}
            name = labels.get("__name__", OTLP_PROBE_METRIC)
            rendered = ", ".join(
                f'{key}="{value}"' for key, value in sorted(labels.items()) if key != "__name__"
            )
            waited = time.monotonic() - post_started
            detail = (
                f"OTLP/HTTP 200 (partialSuccess) but unattributed {name}{{{rendered}}} "
                f"was stored after {waited:.1f}s; an app without service.name must be rejected"
            )
            return Result("otlp_service_name", FAIL, detail)
        if time.monotonic() >= deadline:
            break
        time.sleep(OTLP_PROBE_INTERVAL_S)
    detail = (
        f"OTLP/HTTP 200 (partialSuccess) and no series for probe={probe} in Prometheus "
        f"after {OTLP_SERVICE_NAME_WINDOW_S:g}s — unattributed datapoint dropped"
    )
    return Result("otlp_service_name", PASS, detail)


def check_service_label(name, prometheus, timeout):
    """Prove the scrape-time relabel stamped every application series."""
    job_total, error = prometheus_count(
        prometheus, f'count({{job="{name}", __name__!~"{SYNTHETIC_SERIES}"}})', timeout
    )
    if error:
        return Result("service_label", FAIL, error)
    stamped, error = prometheus_count(
        prometheus, f'count({{job="{name}", service="{name}"}})', timeout
    )
    if error:
        return Result("service_label", FAIL, error)
    raw_total, _ = prometheus_count(prometheus, f'count({{job="{name}"}})', timeout)

    if raw_total == 0:
        detail = f'prometheus has no series for job "{name}" (service is not scraped)'
        return Result("service_label", FAIL, detail)
    synthetic = (raw_total - job_total) if raw_total is not None else 0
    evidence = (
        f'prometheus job "{name}": {stamped:g}/{job_total:g} application series carry '
        f'service="{name}"'
    )
    if synthetic > 0:
        evidence += f" (count({{job}}) = {raw_total:g} incl. {synthetic:g} synthetic up/scrape_*)"
    ok = job_total > 0 and stamped > 0 and stamped == job_total
    return Result("service_label", PASS if ok else FAIL, evidence)


def check_route_label(samples, allowlist=None, descriptor_error=None, declared_label=None):
    if samples is None:
        return Result("route_label", FAIL, "GET /metrics unavailable; route label not evaluable")

    present = {
        key for _, labels in samples if "status" in labels for key in ROUTE_LABELS if key in labels
    }
    if declared_label in present:
        label = declared_label
    elif present:
        label = sorted(present)[0]
    else:
        detail = "no request counter carries both a status label and a route/endpoint label"
        return Result("route_label", FAIL, detail)

    series = [(n, labels) for n, labels in samples if "status" in labels and label in labels]
    values = sorted({labels[label] for _, labels in series})
    names = sorted({n for n, _ in series})
    shown = values[:8]
    evidence = f'label "{label}" on {names[0]}'
    if len(names) > 1:
        evidence += f" (+{len(names) - 1} more)"
    evidence += f": {len(values)} distinct value(s) {', '.join(shown)}"
    if len(values) > len(shown):
        evidence += f", … (+{len(values) - len(shown)} more)"

    if len(values) > ROUTE_VALUE_LIMIT:
        evidence = f"{len(values)} distinct values exceed {ROUTE_VALUE_LIMIT}: {evidence}"
        return Result("route_label", FAIL, evidence)

    if descriptor_error:
        return Result("route_label", FAIL, f"{descriptor_error}; route values cannot be checked")

    if allowlist is None:
        # No allowlist is a statement, not a gap: check the two violations that
        # need no allowlist (raw IDs in route values, and cardinality blow-up).
        raw = [
            value
            for value in values
            if looks_like_raw_id(value) and not is_infrastructure_route(normalise_route(value))
        ]
        if raw:
            detail = f"{len(raw)} raw-ID route value(s) with routes_allowlist not declared: "
            detail += ", ".join(raw[:6])
            if len(raw) > 6:
                detail += f", … (+{len(raw) - 6} more)"
            return Result("route_label", FAIL, f"{detail}; {evidence}")
        evidence += "; routes_allowlist not declared; checked raw IDs and cardinality only"
        return Result("route_label", PASS, evidence)

    if not isinstance(allowlist, list):
        return Result(
            "route_label",
            FAIL,
            "descriptor has no usable routes_allowlist; route values cannot be checked",
        )
    allowed = {
        normalise_route(route) for route in allowlist if isinstance(route, str) and route.strip()
    }
    allowed |= INFRASTRUCTURE_ROUTES
    uncovered = [
        value
        for value in values
        if not is_infrastructure_route(normalise_route(value))
        and normalise_route(value) not in allowed
    ]
    if uncovered:
        raw = [value for value in uncovered if looks_like_raw_id(value)]
        detail = f"{len(uncovered)} observed route value(s) not in routes_allowlist: "
        detail += ", ".join(uncovered[:6])
        if len(uncovered) > 6:
            detail += f", … (+{len(uncovered) - 6} more)"
        if raw:
            detail += f" (raw ID: {', '.join(raw[:3])})"
        detail += f"; allowlist: {', '.join(sorted(allowed - INFRASTRUCTURE_ROUTES)) or 'empty'}"
        return Result("route_label", FAIL, detail)
    evidence += "; all values covered by routes_allowlist (or infrastructure/control-plane routes)"
    return Result("route_label", PASS, evidence)


def check_series_budget(samples):
    if samples is None:
        return Result(
            "series_budget", FAIL, "GET /metrics unavailable; series budget not evaluable"
        )
    series = {(name, tuple(sorted(labels.items()))) for name, labels in samples}
    app = {entry for entry in series if not entry[0].startswith(EXEMPT_PREFIXES)}
    exempt = len(series) - len(app)
    evidence = f"{len(app)} application series (limit {SERIES_LIMIT}), {exempt} runtime-exempt"
    verdict = PASS if len(app) <= SERIES_LIMIT else FAIL
    return Result("series_budget", verdict, evidence)


def prometheus_named_counts(prometheus, query, timeout):
    """Run an instant count-by-name query; return ({metric_name: count}, error)."""
    params = urllib.parse.urlencode({"query": query})
    status, body, _, error = fetch(f"{prometheus}/api/v1/query?{params}", timeout)
    if error:
        return None, f"prometheus unreachable: {error}"
    if status != 200:
        return None, f"prometheus query -> HTTP {status}: {body[:100].strip()}"
    try:
        payload = json.loads(body)
        if payload.get("status") != "success":
            return None, f"prometheus error: {payload.get('error', 'unknown')}"
        counts = {}
        for sample in payload["data"]["result"]:
            counts[sample["metric"]["__name__"]] = int(float(sample["value"][1]))
        return counts, None
    except (ValueError, KeyError, TypeError) as exc:
        return None, f"prometheus response unreadable: {type(exc).__name__}"


def prometheus_metric_labels(prometheus, job, metric, timeout):
    """Label names on the metric's current series for this job, or an empty set."""
    query = f'{metric}{{job="{job}"}}'
    params = urllib.parse.urlencode({"query": query})
    status, body, _, error = fetch(f"{prometheus}/api/v1/query?{params}", timeout)
    if error or status != 200:
        return set()
    try:
        result = json.loads(body)["data"]["result"]
    except (ValueError, KeyError, TypeError):
        return set()
    names = set()
    for sample in result:
        names.update(sample.get("metric", {}))
    names.discard("__name__")
    return names


def check_series_integrity(name, samples, prometheus, timeout):
    """Stored series must not be a collapsed subset of what the app exposes.

    ``labelkeep`` can only drop labels, and dropping a label merges every raw
    series that differed only in that label. Prometheus keeps one sample per
    timestamp, so the extra series vanish silently. Comparing the exposed
    distinct label sets per metric against ``count by (__name__)`` catches it;
    ``scrape_samples_scraped`` does not, because it counts samples, not surviving
    label sets. ``generation`` on python_gc_* is the concrete case.
    """
    if samples is None:
        detail = "GET /metrics unavailable; series integrity not evaluable"
        return Result("series_integrity", FAIL, detail)

    exposed = {}
    for metric, labels in samples:
        exposed.setdefault(metric, set()).add(tuple(sorted(labels.items())))
    counts, error = prometheus_named_counts(
        prometheus, f'count by (__name__) ({{job="{name}"}})', timeout
    )
    if error:
        return Result("series_integrity", FAIL, error)

    problems = []
    for metric in sorted(exposed):
        raw_count = len(exposed[metric])
        stored = counts.get(metric, 0)
        if stored >= raw_count:
            continue
        kept = prometheus_metric_labels(prometheus, name, metric, timeout)
        dropped = {key for pair in exposed[metric] for key, _ in pair} - kept
        detail = f"{metric}: {raw_count} exposed -> {stored} stored"
        if dropped:
            dimensions = []
            for key in sorted(dropped):
                values = {value for pair in exposed[metric] for k, value in pair if k == key}
                shown = ", ".join(sorted(values)[:4])
                if len(values) > 4:
                    shown += ", …"
                dimensions.append(f"{key} ({shown})")
            detail += "; dropped " + ", ".join(dimensions[:3])
        problems.append(detail)

    raw_total = sum(len(series) for series in exposed.values())
    stored_total = sum(counts.get(metric, 0) for metric in exposed)
    evidence = f"{len(exposed)} metric names: {raw_total} exposed series, {stored_total} stored"
    if not problems:
        return Result("series_integrity", PASS, evidence)
    detail = f"{len(problems)} metric name(s) collapsed: " + "; ".join(problems[:3])
    if len(problems) > 3:
        detail += f"; … (+{len(problems) - 3} more)"
    return Result("series_integrity", FAIL, f"{detail} ({evidence})")


def check_healthz(base, timeout):
    # Allow twice the budget so a slow-but-answering server is measured, not
    # reported as a bare timeout.
    status, _, elapsed, error = fetch(f"{base}/healthz", min(timeout, HEALTH_BUDGET_S * 2))
    if error:
        return Result("healthz", FAIL, f"GET /healthz failed: {error}")
    if status == 404:
        h_status, _, h_elapsed, h_error = fetch(f"{base}/health", min(timeout, 2.0))
        detail = (
            f"GET /health also failed: {h_error}"
            if h_error
            else (f"GET /health -> HTTP {h_status} in {h_elapsed:.3f}s")
        )
        evidence = f"GET /healthz -> 404 (missing); {detail}"
        return Result("healthz", FAIL, evidence + " (contract requires /healthz, not /health)")
    if status != 200:
        return Result("healthz", FAIL, f"GET /healthz -> HTTP {status} in {elapsed:.3f}s")
    if elapsed >= HEALTH_BUDGET_S:
        return Result(
            "healthz", FAIL, f"GET /healthz -> 200 in {elapsed:.3f}s (budget {HEALTH_BUDGET_S:g}s)"
        )
    return Result("healthz", PASS, f"GET /healthz -> 200 in {elapsed:.3f}s")


def check_otlp_traces(name, timeout):
    end = int(time.time())
    params = urllib.parse.urlencode(
        {"tags": f"service.name={name}", "limit": 5, "start": end - TRACE_LOOKBACK_S, "end": end}
    )
    status, body, _, error = fetch(f"{TEMPO_URL}/api/search?{params}", timeout)
    if error:
        return Result("otlp_traces", SKIP, f"tempo unreachable: {error}")
    if status != 200:
        return Result("otlp_traces", SKIP, f"tempo search -> HTTP {status}: {body[:100].strip()}")
    try:
        traces = json.loads(body).get("traces") or []
    except (ValueError, AttributeError):
        detail = f"tempo search -> 200 but unreadable body: {body[:80]!r}"
        return Result("otlp_traces", SKIP, detail)
    window = TRACE_LOOKBACK_S // 60
    where = f"tempo search service.name={name} last {window}m"
    if not traces:
        return Result("otlp_traces", FAIL, f"{where} -> 0 traces")
    newest = traces[0].get("rootTraceName", "?") if isinstance(traces[0], dict) else "?"
    evidence = f"{where} -> {len(traces)} traces, newest {newest}"
    return Result("otlp_traces", PASS, evidence)


def check_trace_id_log_format(name, timeout):
    end_ns = time.time_ns()
    query = f'{{compose_service="{name}"}}'
    params = urllib.parse.urlencode(
        {"query": query, "limit": 200, "start": end_ns - LOG_LOOKBACK_NS, "end": end_ns}
    )
    status, body, _, error = fetch(f"{LOKI_URL}/loki/api/v1/query_range?{params}", timeout)
    if error:
        return Result("trace_id_log_format", FAIL, f"loki query failed: {error}")
    if status != 200:
        return Result(
            "trace_id_log_format", FAIL, f"loki query -> HTTP {status}: {body[:100].strip()}"
        )
    try:
        streams = json.loads(body)["data"]["result"]
    except (ValueError, KeyError, TypeError):
        return Result(
            "trace_id_log_format", FAIL, f"loki query -> 200 but unexpected body: {body[:80]!r}"
        )

    lines = [value[1] for stream in streams for value in stream.get("values", []) if len(value) > 1]
    matched = sum(1 for line in lines if TRACE_ID_RE.search(line))
    if lines:
        evidence = (
            f"{matched}/{len(lines)} lines match trace_id=<32 lowercase hex> for {query} (15m)"
        )
    else:
        evidence = f"0/0 lines: no log streams for {query} in last 15m"
    return Result("trace_id_log_format", PASS if matched else FAIL, evidence)


def check_log_continuity(name, timeout, max_gap):
    """Newest line is fresh and no quiet stretch exceeds the gap budget.

    A tail that silently stopped shows up here, which duplicate-based checks
    would miss. Low-traffic services (< 5 lines in the window) are SKIP, not
    FAIL, so a quiet service cannot fail the contract on volume alone.
    """
    end_ns = time.time_ns()
    start_ns = end_ns - LOG_CONTINUITY_WINDOW_S * 1_000_000_000
    query = f'{{compose_service="{name}"}}'
    params = urllib.parse.urlencode(
        {
            "query": query,
            "limit": 5000,
            "start": start_ns,
            "end": end_ns,
            "direction": "backward",
        }
    )
    status, body, _, error = fetch(f"{LOKI_URL}/loki/api/v1/query_range?{params}", timeout)
    if error:
        return Result("log_continuity", FAIL, f"loki query failed: {error}")
    if status != 200:
        return Result("log_continuity", FAIL, f"loki query -> HTTP {status}: {body[:100].strip()}")
    try:
        streams = json.loads(body)["data"]["result"]
        timestamps = [
            int(value[0])
            for stream in streams
            for value in stream.get("values", [])
            if len(value) > 1
        ]
    except (ValueError, KeyError, TypeError):
        return Result(
            "log_continuity", FAIL, f"loki query -> 200 but unexpected body: {body[:80]!r}"
        )

    window = LOG_CONTINUITY_WINDOW_S // 60
    if len(timestamps) < LOG_CONTINUITY_MIN_LINES:
        detail = (
            f"{len(timestamps)} line(s) for {query} in last {window}m "
            f"(< {LOG_CONTINUITY_MIN_LINES}; low traffic, continuity not evaluable)"
        )
        return Result("log_continuity", SKIP, detail)

    ordered = sorted(timestamps)
    newest_age = (time.time_ns() - ordered[-1]) / 1e9
    largest_gap = max(
        (later - earlier) / 1e9 for earlier, later in zip(ordered, ordered[1:], strict=False)
    )
    evidence = (
        f"{len(timestamps)} lines/{window}m for {query}: newest {newest_age:.0f}s old "
        f"(budget {LOG_CONTINUITY_MAX_AGE_S:g}s), max gap {largest_gap:.1f}s "
        f"(budget {max_gap:g}s)"
    )
    ok = newest_age <= LOG_CONTINUITY_MAX_AGE_S and largest_gap <= max_gap
    return Result("log_continuity", PASS if ok else FAIL, evidence)


def _metric_observed(name, sample_names):
    """A histogram is exposed as <name>_bucket/_sum/_count/_created."""
    return any(sample == name or sample.startswith(name + "_") for sample in sample_names)


def check_descriptor(name, services_dir, samples=None):
    path = os.path.join(services_dir, f"{name}.yaml")
    data, error = read_descriptor(name, services_dir)
    if error:
        return Result("descriptor", FAIL, error)
    problems = validate_descriptor(data, name)
    if problems:
        evidence = f"{path}: {len(problems)} problem(s): " + "; ".join(problems[:8])
        if len(problems) > 8:
            evidence += f"; … (+{len(problems) - 8} more)"
        return Result("descriptor", FAIL, evidence)
    port = data["scrape"]["port"]
    routes = data.get("routes_allowlist")
    if isinstance(routes, list):
        evidence = f"{path}: schema v1 valid (port {port}, {len(routes)} routes)"
    else:
        evidence = f"{path}: schema v1 valid (port {port}, routes_allowlist not declared)"

    metrics = data.get("metrics")
    if isinstance(metrics, dict):
        histogram = data["slos"]["latency"]["histogram"]
        declared = [metrics["requests_total"], histogram]
        if samples is None:
            evidence += "; metrics declared but /metrics unavailable to verify"
        else:
            sample_names = {sample_name for sample_name, _ in samples}
            missing = [m for m in declared if not _metric_observed(m, sample_names)]
            if missing:
                evidence += f"; metrics not observed in scraped series: {', '.join(missing)}"
                return Result("descriptor", FAIL, evidence)
            evidence += (
                f"; metrics: requests_total={metrics['requests_total']} "
                f"latency_histogram={histogram} "
                f"({len(declared)}/{len(declared)} observed)"
            )
    else:
        evidence += "; metrics not declared; RED and SLO are span-only"

    return Result("descriptor", PASS, evidence)


def run_service(name, base, timeout, prometheus, services_dir, max_log_gap):
    metrics_result, samples = check_metrics_reachable(base, timeout)
    descriptor, descriptor_error = read_descriptor(name, services_dir)
    allowlist = descriptor.get("routes_allowlist") if isinstance(descriptor, dict) else None
    metrics = descriptor.get("metrics") if isinstance(descriptor, dict) else None
    declared_label = None
    if isinstance(metrics, dict):
        declared_label = metrics.get("route_label")
    results = [metrics_result]
    results.append(check_service_label(name, prometheus, timeout))
    results.append(check_route_label(samples, allowlist, descriptor_error, declared_label))
    results.append(check_series_budget(samples))
    results.append(check_series_integrity(name, samples, prometheus, timeout))
    results.append(check_healthz(base, timeout))
    results.append(check_otlp_traces(name, timeout))
    results.append(check_trace_id_log_format(name, timeout))
    results.append(check_log_continuity(name, timeout, max_log_gap))
    results.append(check_descriptor(name, services_dir, samples))
    return results


def rank_failures(all_results):
    grouped = {}
    for service, results in all_results.items():
        for result in results:
            if result.verdict == FAIL:
                grouped.setdefault(result.check, []).append((service, result.evidence))
    return sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))


def tally(all_results):
    total = sum(len(results) for results in all_results.values())
    passed = sum(1 for r in all_results.values() for x in r if x.verdict == PASS)
    skipped = sum(1 for r in all_results.values() for x in r if x.verdict == SKIP)
    failed = total - passed - skipped
    return total, passed, skipped, failed


def print_report(bases, all_results, platform_results):
    print("\nplatform  (shared)")
    width = max(len(result.check) for result in platform_results)
    for result in platform_results:
        print(f"  {result.check:<{width}}  {result.verdict:<4}  {result.evidence}")

    for service, base in bases.items():
        results = all_results[service]
        print(f"\n{service}  ({base})")
        width = max(len(result.check) for result in results)
        for result in results:
            print(f"  {result.check:<{width}}  {result.verdict:<4}  {result.evidence}")

    total, passed, skipped, failed = tally(all_results)
    summary = f"{passed}/{total} checks passed"
    if skipped:
        summary += f", {skipped} skipped"
    if failed:
        summary += f", {failed} failed"
    print(f"\n{summary}")

    failures = rank_failures(all_results)
    if failures:
        print("\nFailures (ranked by number of services affected):")
        for rank, (check, hits) in enumerate(failures, 1):
            print(f"  {rank}. {check} — {len(hits)}/{len(bases)} services")
            for service, evidence in hits:
                print(f"       {service}: {evidence}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check services against the argus platform contract v1."
    )
    parser.add_argument(
        "--service",
        action="append",
        default=[],
        metavar="NAME=BASE_URL",
        help="service name and base URL, repeatable",
    )
    parser.add_argument("--json", action="store_true", help="emit one JSON object instead of text")
    parser.add_argument(
        "--timeout", type=float, default=5.0, help="per-request timeout (default 5)"
    )
    parser.add_argument(
        "--prometheus",
        default=DEFAULT_PROMETHEUS_URL,
        help=f"Prometheus base URL (default {DEFAULT_PROMETHEUS_URL})",
    )
    parser.add_argument(
        "--collector",
        default=DEFAULT_COLLECTOR_URL,
        help=f"OTLP/HTTP collector base URL (default {DEFAULT_COLLECTOR_URL})",
    )
    parser.add_argument(
        "--services-dir",
        default="services",
        help="directory holding <name>.yaml descriptors (default services)",
    )
    parser.add_argument(
        "--max-log-gap",
        type=float,
        default=DEFAULT_MAX_LOG_GAP_S,
        metavar="SECONDS",
        help=(
            f"largest allowed gap between consecutive log lines (default {DEFAULT_MAX_LOG_GAP_S:g})"
        ),
    )
    args = parser.parse_args(argv)

    bases = {}
    for spec in args.service:
        name, sep, url = spec.partition("=")
        if not sep or not name or not url:
            parser.error(f"--service expects NAME=BASE_URL, got {spec!r}")
        bases[name] = url.rstrip("/")
    if not bases:
        parser.error("at least one --service NAME=BASE_URL is required")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.max_log_gap <= 0:
        parser.error("--max-log-gap must be positive")

    prometheus = args.prometheus.rstrip("/")
    collector = args.collector.rstrip("/")
    platform_results = []
    for check, runner in (
        ("otlp_metrics", check_otlp_metrics),
        ("otlp_service_name", check_otlp_service_name),
        ("span_metrics", check_span_metrics),
    ):
        try:
            platform_results.append(runner(collector, prometheus, args.timeout))
        except Exception as exc:  # never traceback on an unexpected failure
            platform_results.append(Result(check, FAIL, f"{type(exc).__name__}: {exc}"))

    all_results = {}
    for name, base in bases.items():
        try:
            all_results[name] = run_service(
                name, base, args.timeout, prometheus, args.services_dir, args.max_log_gap
            )
        except Exception as exc:  # never traceback on an unexpected failure
            all_results[name] = [Result("internal_error", FAIL, f"{type(exc).__name__}: {exc}")]

    failures = rank_failures(all_results)
    total, passed, skipped, failed = tally(all_results)
    platform_failed = any(result.verdict == FAIL for result in platform_results)
    exit_code = 1 if failed or platform_failed else 0

    if args.json:
        payload = {
            "contract": "v1",
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "prometheus": prometheus,
            "collector": collector,
            "services_dir": args.services_dir,
            "platform": {result.check: asdict(result) for result in platform_results},
            "services": {
                name: {
                    "base_url": bases[name],
                    "checks": [asdict(result) for result in all_results[name]],
                }
                for name in bases
            },
            "summary": {
                "passed": passed,
                "total": total,
                "skipped": skipped,
                "failed": failed,
                "exit_code": exit_code,
            },
            "failures_ranked": [
                {
                    "check": check,
                    "services_affected": len(hits),
                    "details": [{"service": s, "evidence": e} for s, e in hits],
                }
                for check, hits in failures
            ],
        }
        print(json.dumps(payload, indent=2))
    else:
        print_report(bases, all_results, platform_results)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

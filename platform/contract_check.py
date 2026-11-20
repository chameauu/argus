#!/usr/bin/env python3
"""Conformance checker for the argus platform contract v1 (phase 0).

Five checks per service, each PASS / FAIL / SKIP with quoted evidence:
  metrics_reachable, service_label, healthz, otlp_traces, trace_id_log_format

Usage:
  python3 platform/contract_check.py \
      --service target-app=http://localhost:18000 \
      --service custom-exporter=http://localhost:19200 \
      --service webhook-receiver=http://localhost:18090 [--json] [--timeout 5]

Exit code: 0 when every check is PASS or SKIP, 1 when any check FAILs.
Standard library only.
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
from dataclasses import asdict, dataclass

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

TEMPO_URL = os.environ.get("ARGUS_TEMPO_URL", "http://localhost:13200").rstrip("/")
LOKI_URL = os.environ.get("ARGUS_LOKI_URL", "http://localhost:13100").rstrip("/")

# Runtime/client-library families are exempt from the service label rule.
EXEMPT_PREFIXES = ("go_", "python_", "process_", "promhttp_")
HEALTH_BUDGET_S = 1.0
SERIES_LIMIT = 500
TRACE_LOOKBACK_S = 15 * 60
LOG_LOOKBACK_NS = 15 * 60 * 1_000_000_000
TRACE_ID_RE = re.compile(r"trace_id=[0-9a-f]{32}")
SAMPLE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})?\s+(\S+)(?:\s+\S+)?(?:\s+#.*)?$")
LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


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


def check_service_label(name, samples):
    app = [(n, labels) for n, labels in samples if not n.startswith(EXEMPT_PREFIXES)]
    if not app:
        detail = f"0 app series among {len(samples)} exposed (all are runtime-exempt families)"
        return Result("service_label", FAIL, detail)
    carrying = [(n, labels) for n, labels in app if labels.get("service") == name]
    total = len(app)
    evidence = f'{len(carrying)}/{total} app series carry service="{name}"'
    if len(carrying) != total:
        missing = sorted({n for n, labels in app if labels.get("service") != name})
        evidence += f" ({total - len(carrying)} lack it, e.g. {', '.join(missing[:3])})"
    return Result("service_label", PASS if carrying and len(carrying) == total else FAIL, evidence)


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

    lines = [v[1] for s in streams for v in s.get("values", []) if len(v) > 1]
    matched = sum(1 for line in lines if TRACE_ID_RE.search(line))
    if lines:
        evidence = (
            f"{matched}/{len(lines)} lines match trace_id=<32 lowercase hex> for {query} (15m)"
        )
    else:
        evidence = f"0/0 lines: no log streams for {query} in last 15m"
    return Result("trace_id_log_format", PASS if matched else FAIL, evidence)


def run_service(name, base, timeout):
    metrics_result, samples = check_metrics_reachable(base, timeout)
    results = [metrics_result]
    if samples is None:
        results.append(
            Result(
                "service_label",
                SKIP,
                "/metrics unavailable or unparseable; label check not evaluable",
            )
        )
    else:
        results.append(check_service_label(name, samples))
    results.append(check_healthz(base, timeout))
    results.append(check_otlp_traces(name, timeout))
    results.append(check_trace_id_log_format(name, timeout))
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


def print_report(bases, all_results):
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

    all_results = {}
    for name, base in bases.items():
        try:
            all_results[name] = run_service(name, base, args.timeout)
        except Exception as exc:  # never traceback on an unexpected failure
            all_results[name] = [Result("internal_error", FAIL, f"{type(exc).__name__}: {exc}")]

    failures = rank_failures(all_results)
    total, passed, skipped, failed = tally(all_results)
    exit_code = 1 if failed else 0

    if args.json:
        payload = {
            "contract": "v1",
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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
        print_report(bases, all_results)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

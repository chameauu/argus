#!/usr/bin/env python3
"""Render the argus platform artefacts from service descriptors.

Every per-service output is derived from ``services/<name>.yaml`` through a
``string.Template`` in ``platform/templates/``. The descriptor parser and
validator are reused from ``contract_check.py``; this module does not carry a
second YAML reader.

Generated:
  docker-compose/configs/prometheus/prometheus.yml   whole file
  alert-rules/generated/red-<name>.yml                RED rules
  runbooks/generated/<name>.md                        runbook stub

Dashboards are owned by phase 2b and are intentionally not generated here.

Usage:
  python3 platform/generate.py [--services-dir services] [--out .] [--check]

``--check`` renders into a temporary directory and compares bytes with the
tree. Exit 0 means no drift; exit 1 prints a per-file summary.

Standard library only. Output is deterministic: sorted services, LF endings,
no timestamps, no absolute paths.
"""

import argparse
import re
import sys
import tempfile
from pathlib import Path
from string import Template

from contract_check import YamlError, parse_descriptor, validate_descriptor

PLATFORM_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PLATFORM_DIR / "templates"

PROMETHEUS_OUTPUT = "docker-compose/configs/prometheus/prometheus.yml"
RULES_DIR = "alert-rules/generated"
RUNBOOKS_DIR = "runbooks/generated"

# Keep the rendered scrape_configs stable across runs.
NAME_PART_RE = re.compile(r"[-_.]+")


class GenerateError(RuntimeError):
    """A descriptor or template is unusable; the run must fail loudly."""


def read_template(name):
    try:
        return Template((TEMPLATE_DIR / name).read_text(encoding="utf-8"))
    except OSError as exc:
        raise GenerateError(f"template {name}: {exc}") from exc


def pascal_case(name):
    return "".join(part[:1].upper() + part[1:] for part in NAME_PART_RE.split(name) if part)


def format_number(value):
    return f"{value:g}"


def format_percent(value):
    return f"{value * 100:g}%"


def load_services(services_dir):
    directory = Path(services_dir)
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        raise GenerateError(f"no descriptors in {directory}")
    services = []
    for path in paths:
        name = path.stem
        try:
            data = parse_descriptor(path.read_text(encoding="utf-8"))
        except (OSError, YamlError) as exc:
            raise GenerateError(f"{path}: unparseable: {exc}") from exc
        problems = validate_descriptor(data, name)
        if problems:
            details = "; ".join(problems[:8])
            raise GenerateError(f"{path}: invalid descriptor: {details}")
        services.append((name, data))
    return services


def service_context(name, data):
    scrape = data["scrape"]
    health = data["health"]
    slos = data["slos"]
    availability = slos["availability"]
    latency = slos["latency"]
    metrics = data.get("metrics") or {}
    routes = data["routes_allowlist"]
    route_label = metrics.get("route_label", "route")
    threshold = format_number(latency["threshold_seconds"])
    return {
        "name": name,
        "owner": data["owner"],
        "namespace": data["namespace"],
        "environment": data["environment"],
        "target": f"{name}:{scrape['port']}",
        "metrics_path": scrape.get("path", "/metrics"),
        "interval": scrape.get("interval", "15s"),
        "health_path": health.get("path", "/healthz"),
        "route_count": str(len(routes)),
        "routes": ", ".join(f"`{route}`" for route in routes) or "none",
        "availability_objective": format_percent(availability["objective"]),
        "availability_window": availability["window"],
        "error_selector": availability["error_selector"],
        "latency_objective": format_percent(latency["objective"]),
        "threshold": threshold,
        "threshold_seconds": threshold,
        "route_label": route_label,
        "requests_metric": metrics.get("requests_total", "not declared"),
        "latency_metric": metrics.get("latency_histogram", "not declared"),
        "latency_bucket": (
            f"{metrics['latency_histogram']}_bucket" if metrics else "not_declared_bucket"
        ),
        "down_alert": f"{pascal_case(name)}Down",
        "error_alert": f"{pascal_case(name)}HighErrorRate",
        "latency_alert": f"{pascal_case(name)}HighLatency",
        "generated_runbook": f"{RUNBOOKS_DIR}/{name}.md",
    }


def render_app_job(context):
    return read_template("app-job.yml.tmpl").substitute(context).rstrip("\n")


def render_prometheus(services):
    jobs = [
        render_app_job(service_context(name, data))
        for name, data in sorted(services, key=lambda item: item[0])
    ]
    body = read_template("prometheus.yml.tmpl").substitute(app_jobs="\n\n".join(jobs))
    return body.rstrip("\n") + "\n"


def render_rules(name, data):
    context = service_context(name, data)
    template = "red-rules.yml.tmpl" if data.get("metrics") else "red-down.yml.tmpl"
    body = read_template(template).substitute(context)
    return body.rstrip("\n") + "\n"


def render_runbook(name, data):
    body = read_template("runbook.md.tmpl").substitute(service_context(name, data))
    return body.rstrip("\n") + "\n"


def render_all(services):
    rendered = {PROMETHEUS_OUTPUT: render_prometheus(services)}
    for name, data in services:
        rendered[f"{RULES_DIR}/red-{name}.yml"] = render_rules(name, data)
        rendered[f"{RUNBOOKS_DIR}/{name}.md"] = render_runbook(name, data)
    return dict(sorted(rendered.items()))


def write_all(rendered, out_dir):
    root = Path(out_dir)
    for relative, content in rendered.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content.encode("utf-8"))
        print(f"wrote {relative} ({len(content)} bytes)")


def diff_summary(relative, expected, actual):
    expected_lines = expected.splitlines()
    actual_lines = actual.splitlines()
    removed = sum(1 for line in actual_lines if line not in expected_lines)
    added = sum(1 for line in expected_lines if line not in actual_lines)
    return f"CHANGED {relative} (+{added}/-{removed} lines vs rendered)"


def check_all(temp_dir, out_dir, relative_paths):
    temp_root = Path(temp_dir)
    root = Path(out_dir)
    drift = []
    for relative in relative_paths:
        expected = (temp_root / relative).read_text(encoding="utf-8")
        destination = root / relative
        if not destination.is_file():
            drift.append(f"MISSING {relative}")
            continue
        try:
            actual = destination.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            drift.append(f"UNREADABLE {relative}: {type(exc).__name__}")
            continue
        if actual != expected:
            drift.append(diff_summary(relative, expected, actual))
    if drift:
        print(f"generate: {len(drift)} file(s) differ")
        for line in drift:
            print(f"  {line}")
        return 1
    print(f"generate: no drift ({len(relative_paths)} files)")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Render argus platform artefacts from services/*.yaml descriptors."
    )
    parser.add_argument(
        "--services-dir", default="services", help="descriptor directory (default services)"
    )
    parser.add_argument("--out", default=".", help="output root (default .)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="render to a temp dir and diff against --out; exit 1 on drift",
    )
    args = parser.parse_args(argv)

    try:
        services = load_services(args.services_dir)
        rendered = render_all(services)
    except GenerateError as exc:
        print(f"generate: {exc}", file=sys.stderr)
        return 2

    if args.check:
        with tempfile.TemporaryDirectory(prefix="argus-generate-") as temp_dir:
            write_all_to_temp(rendered, temp_dir)
            return check_all(temp_dir, args.out, rendered.keys())

    write_all(rendered, args.out)
    return 0


def write_all_to_temp(rendered, temp_dir):
    """Materialise the render so --check can be self-tested if needed."""
    root = Path(temp_dir)
    for relative, content in rendered.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content.encode("utf-8"))


if __name__ == "__main__":
    sys.exit(main())

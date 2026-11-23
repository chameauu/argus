#!/usr/bin/env python3
"""Descriptor schema and parser for the argus platform contract v1.

This is the single parser for ``services/<name>.yaml``. It is standard library
only: no network, no Docker, no stack. ``contract_lint.py`` validates
descriptors offline and ``contract_live.py`` imports the same parser for its
``descriptor`` check, so the two tools cannot drift apart.

Exports:
  parse_descriptor(text)                 minimal YAML reader -> dict
  validate_descriptor(data, name)        schema v1 rules -> list of problems
  load_services(services_dir)            parse + validate every *.yaml
  read_descriptor(name, services_dir)    (data, error) for one descriptor

Required keys: name, owner, namespace, environment, scrape, health, runbook.
Optional enrichment: metrics, slos, routes_allowlist. ``metrics`` and ``slos``
are all-or-nothing; an empty ``routes_allowlist`` is rejected because absence is
how a service declares it has none; a declared ``slos.latency`` requires an
allowlist, since that SLI filters on it.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

_MISSING = object()

DESCRIPTOR_REQUIRED_KEYS = (
    "name",
    "owner",
    "namespace",
    "environment",
    "scrape",
    "health",
    "runbook",
)
# Schema v1 amendment 2026-11-24: the metric contract is optional enrichment.
# Span-derived RED covers every traced service, so a descriptor may stop at
# name/owner/namespace/environment/scrape/health/runbook. Declaring `metrics`
# also declares `slos` (and a latency SLI needs its `routes_allowlist`).
DESCRIPTOR_OPTIONAL_KEYS = ("metrics", "slos", "routes_allowlist")
DESCRIPTOR_TOP_KEYS = DESCRIPTOR_REQUIRED_KEYS + DESCRIPTOR_OPTIONAL_KEYS
METRIC_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
ROUTE_LABELS = ("route", "endpoint")
# Liveness, scrape and API-doc endpoints are platform traffic, not application
# routes, so they never need to appear in a descriptor's routes_allowlist.
INFRASTRUCTURE_ROUTES = frozenset(
    {"/health", "/healthz", "/metrics", "/openapi.json", "/docs", "/redoc", "unmatched"}
)
# Control-plane endpoints (fault injection, overrides) are operator traffic. They
# are excluded from the allowlist comparison for the same reason as the routes
# above, and deliberately NOT added to descriptors: routes_allowlist also filters
# the latency SLI, so listing them would pull control traffic into the SLO.
CONTROL_ROUTE_PREFIXES = ("/admin/",)
RAW_ID_SEGMENT_RE = re.compile(r"[0-9a-fA-F]{32}")
INT_RE = re.compile(r"[-+]?\d+")
FLOAT_RE = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")


def is_infrastructure_route(value: str) -> bool:
    """True for liveness, scrape, API-doc and control-plane routes."""
    return value in INFRASTRUCTURE_ROUTES or value.startswith(CONTROL_ROUTE_PREFIXES)


def looks_like_raw_id(value):
    """True when any path segment is all digits or a full 32-hex identifier."""
    for segment in value.split("/"):
        segment = segment.strip()
        if segment.isdigit() or RAW_ID_SEGMENT_RE.fullmatch(segment):
            return True
    return False


def normalise_route(value):
    """Normalise a route for comparison without erasing its template shape.

    Trailing slashes are cosmetic; `/orders/{id}` and `/orders/{order_id}` stay
    distinct because the template parameter name is part of the contract.
    """
    value = value.strip()
    if len(value) > 1:
        value = value.rstrip("/")
    return value or "/"


class YamlError(ValueError):
    """Raised when a descriptor cannot be parsed by the minimal YAML reader."""


def _strip_comment(line):
    out = []
    quote = None
    for char in line:
        if quote:
            out.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            out.append(char)
            continue
        if char == "#" and (not out or out[-1].isspace()):
            break
        out.append(char)
    return "".join(out).rstrip()


def _tokenize(text):
    lines = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        if "\t" in stripped[: indent + 1]:
            raise YamlError(f"line {lineno}: tab indentation is not supported")
        lines.append((indent, stripped.strip(), lineno))
    return lines


def _split_flow(text):
    parts, depth, quote, start = [], 0, None, 0
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


def _parse_scalar(text, lineno):
    text = text.strip()
    if not text:
        return None
    if text[0] in "{[":
        return _parse_flow(text, lineno)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "~"):
        return None
    if INT_RE.fullmatch(text):
        return int(text)
    if FLOAT_RE.fullmatch(text):
        return float(text)
    return text


def _parse_flow(text, lineno):
    text = text.strip()
    if text.startswith("{"):
        if not text.endswith("}"):
            raise YamlError(f"line {lineno}: unterminated flow mapping")
        body = text[1:-1].strip()
        if not body:
            return {}
        mapping = {}
        for part in _split_flow(body):
            key, sep, value = part.partition(":")
            if not sep or not key.strip():
                raise YamlError(f"line {lineno}: malformed flow mapping entry {part!r}")
            if key.strip() in mapping:
                raise YamlError(f"line {lineno}: duplicate key {key.strip()!r}")
            mapping[key.strip()] = _parse_scalar(value, lineno)
        return mapping
    if not text.endswith("]"):
        raise YamlError(f"line {lineno}: unterminated flow sequence")
    body = text[1:-1].strip()
    if not body:
        return []
    return [_parse_scalar(part, lineno) for part in _split_flow(body)]


def _parse_mapping(lines, index, indent):
    mapping = {}
    while index < len(lines):
        line_indent, content, lineno = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise YamlError(f"line {lineno}: unexpected indentation")
        if content.startswith("-"):
            break
        key, sep, rest = content.partition(":")
        if not sep or not key.strip():
            raise YamlError(f"line {lineno}: expected 'key: value', got {content!r}")
        key = key.strip()
        if key in mapping:
            raise YamlError(f"line {lineno}: duplicate key {key!r}")
        index += 1
        if rest.strip():
            mapping[key] = _parse_scalar(rest, lineno)
        elif index < len(lines) and lines[index][0] > indent:
            mapping[key], index = _parse_block(lines, index, lines[index][0])
        elif index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("-"):
            mapping[key], index = _parse_sequence(lines, index, indent)
        else:
            mapping[key] = None
    return mapping, index


def _parse_sequence(lines, index, indent):
    items = []
    while index < len(lines):
        line_indent, content, lineno = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent or not content.startswith("-"):
            raise YamlError(f"line {lineno}: malformed sequence entry {content!r}")
        rest = content[1:].strip()
        index += 1
        if not rest:
            if index < len(lines) and lines[index][0] > indent:
                value, index = _parse_block(lines, index, lines[index][0])
            else:
                value = None
        else:
            value = _parse_scalar(rest, lineno)
        items.append(value)
    return items, index


def _parse_block(lines, index, indent):
    if lines[index][1].startswith("-"):
        return _parse_sequence(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def parse_descriptor(text):
    lines = _tokenize(text)
    if not lines:
        return {}
    if lines[0][0] != 0:
        raise YamlError(f"line {lines[0][2]}: top-level keys must not be indented")
    data, index = _parse_block(lines, 0, 0)
    if index != len(lines):
        raise YamlError(f"line {lines[index][2]}: trailing content")
    return data


def _is_number(value):
    return isinstance(value, int | float) and not isinstance(value, bool)


def _check_string(problems, mapping, key, path):
    value = mapping.get(key, _MISSING)
    if value is _MISSING:
        problems.append(f"{path}: missing")
    elif not isinstance(value, str) or not value.strip():
        problems.append(f"{path}: expected non-empty string")
    return value


def _check_mapping(problems, mapping, key, path):
    value = mapping.get(key, _MISSING)
    if value is _MISSING:
        problems.append(f"{path}: missing")
        return None
    if not isinstance(value, dict):
        problems.append(f"{path}: expected mapping")
        return None
    return value


def validate_descriptor(data, service_name):
    problems = []
    if not isinstance(data, dict):
        return ["top level: expected mapping"]

    for key in data:
        if key not in DESCRIPTOR_TOP_KEYS:
            problems.append(f"{key}: unknown top-level key")
    for key in DESCRIPTOR_REQUIRED_KEYS:
        if key not in data:
            problems.append(f"{key}: missing")

    # The metric contract is all-or-nothing: the RED latency rule and the Sloth
    # spec read slos.latency, while the Sloth rules are only rendered when a
    # metric contract exists. Declaring one half would either render nothing or
    # crash the generator, so reject it instead of silently dropping it.
    if ("metrics" in data) != ("slos" in data):
        missing = "slos" if "metrics" in data else "metrics"
        declared = "metrics" if "metrics" in data else "slos"
        problems.append(f"{missing}: required when {declared} is declared")

    for key in ("name", "owner", "namespace", "environment", "runbook"):
        if key in data:
            _check_string(problems, data, key, key)
    if isinstance(data.get("name"), str) and data["name"] != service_name:
        problems.append(f'name: "{data["name"]}" does not match service "{service_name}"')

    scrape = _check_mapping(problems, data, "scrape", "scrape") if "scrape" in data else None
    if scrape is not None:
        port = scrape.get("port", _MISSING)
        if port is _MISSING:
            problems.append("scrape.port: missing")
        elif not isinstance(port, int) or isinstance(port, bool):
            problems.append("scrape.port: expected integer")
        elif not 1 <= port <= 65535:
            problems.append(f"scrape.port: {port} outside 1-65535")
        path = _check_string(problems, scrape, "path", "scrape.path")
        if isinstance(path, str) and not path.startswith("/"):
            problems.append("scrape.path: expected a path starting with '/'")
        _check_string(problems, scrape, "interval", "scrape.interval")

    health = _check_mapping(problems, data, "health", "health") if "health" in data else None
    if health is not None:
        path = _check_string(problems, health, "path", "health.path")
        if isinstance(path, str) and not path.startswith("/"):
            problems.append("health.path: expected a path starting with '/'")

    slos = _check_mapping(problems, data, "slos", "slos") if "slos" in data else None
    if slos is not None:
        availability = _check_mapping(problems, slos, "availability", "slos.availability")
        if availability is not None:
            objective = availability.get("objective", _MISSING)
            if objective is _MISSING:
                problems.append("slos.availability.objective: missing")
            elif not _is_number(objective) or not 0 < objective < 1:
                problems.append("slos.availability.objective: expected number in (0, 1)")
            _check_string(problems, availability, "window", "slos.availability.window")
            _check_string(
                problems, availability, "error_selector", "slos.availability.error_selector"
            )
        latency = _check_mapping(problems, slos, "latency", "slos.latency")
        if latency is not None:
            objective = latency.get("objective", _MISSING)
            if objective is _MISSING:
                problems.append("slos.latency.objective: missing")
            elif not _is_number(objective) or not 0 < objective < 1:
                problems.append("slos.latency.objective: expected number in (0, 1)")
            threshold = latency.get("threshold_seconds", _MISSING)
            if threshold is _MISSING:
                problems.append("slos.latency.threshold_seconds: missing")
            elif not _is_number(threshold) or threshold <= 0:
                problems.append("slos.latency.threshold_seconds: expected positive number")
            # Single source of truth for the latency histogram: the SLI, the RED
            # rules, the dashboard and the descriptor evidence all read this one
            # field. `metrics.latency_histogram` is no longer accepted.
            histogram = _check_string(problems, latency, "histogram", "slos.latency.histogram")
            if isinstance(histogram, str) and not METRIC_NAME_RE.fullmatch(histogram):
                problems.append("slos.latency.histogram: not a valid Prometheus metric name")

    metrics = data.get("metrics", _MISSING)
    if metrics is not _MISSING:
        if not isinstance(metrics, dict):
            problems.append("metrics: expected mapping")
        else:
            allowed = ("requests_total", "route_label")
            for key in metrics:
                if key not in allowed:
                    problems.append(f"metrics.{key}: unknown key")
            value = _check_string(problems, metrics, "requests_total", "metrics.requests_total")
            if isinstance(value, str) and not METRIC_NAME_RE.fullmatch(value):
                problems.append("metrics.requests_total: not a valid Prometheus metric name")
            route_label = metrics.get("route_label", "route")
            if route_label not in ROUTE_LABELS:
                problems.append(f"metrics.route_label: expected one of {', '.join(ROUTE_LABELS)}")

    routes = data.get("routes_allowlist", _MISSING)
    if routes is not _MISSING:
        if not isinstance(routes, list):
            problems.append("routes_allowlist: expected list")
        else:
            for position, route in enumerate(routes):
                if not isinstance(route, str) or not route.strip():
                    problems.append(f"routes_allowlist[{position}]: expected non-empty string")
            # An empty list is a mistake, not a statement: it is indistinguishable
            # from "I meant to list routes and forgot". Absence (the key is not
            # written) is the way to say this service declares no allowlist.
            if not routes:
                problems.append(
                    "routes_allowlist: must not be empty; omit the key to declare no allowlist"
                )
    elif isinstance(slos, dict) and "latency" in slos:
        # The latency SLI filters on this allowlist. Without it the SLI would
        # count every route, including scrape and health traffic, and would be a
        # quieter gate than the descriptor claims.
        problems.append("routes_allowlist: required when slos.latency is declared")
    return problems


def read_descriptor(name, services_dir):
    """Return (data or None, error string or None) for one descriptor."""
    path = os.path.join(services_dir, f"{name}.yaml")
    if not os.path.isfile(path):
        return None, f"no descriptor: {path}"
    try:
        with open(path, encoding="utf-8") as handle:
            return parse_descriptor(handle.read()), None
    except (OSError, YamlError) as exc:
        return None, f"{path}: unparseable: {exc}"


@dataclass(frozen=True)
class ServiceDescriptor:
    """One descriptor and its validation outcome."""

    name: str
    path: str
    data: dict | None
    problems: tuple[str, ...] = ()
    error: str | None = None

    @property
    def valid(self) -> bool:
        return self.error is None and not self.problems


class DescriptorError(ValueError):
    """Raised when a descriptor directory holds no ``*.yaml`` files."""


def load_services(services_dir):
    """Parse and validate every ``*.yaml`` in the directory, sorted by name.

    Per-file failures are reported, not raised, so one broken descriptor cannot
    hide the others. An empty directory is an error: a lint that passes on
    nothing is not a gate.
    """
    directory = Path(services_dir)
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        raise DescriptorError(f"no descriptors in {directory}")
    descriptors = []
    for path in paths:
        name = path.stem
        try:
            data = parse_descriptor(path.read_text(encoding="utf-8"))
        except (OSError, YamlError) as exc:
            descriptors.append(
                ServiceDescriptor(name, str(path), None, error=f"unparseable: {exc}")
            )
            continue
        descriptors.append(
            ServiceDescriptor(name, str(path), data, tuple(validate_descriptor(data, name)))
        )
    return descriptors

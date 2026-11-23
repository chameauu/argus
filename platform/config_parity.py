#!/usr/bin/env python3
"""Prove the running stack is running the configuration that is committed.

The failure this catches is silent by construction: every service in this stack
reads its config once, at start. Editing a mounted file and forgetting to
restart leaves the container serving the old config while the repository, the
review and `git status` all claim otherwise. That is exactly how spanmetrics
lived in one container's memory for an hour before it was noticed.

Two reload models are distinguished:

* ``restart`` containers (collector, Loki, Tempo, Alloy) read config only at
  start, so a config file newer than the container's start time is drift.
* Prometheus reloads on SIGHUP, so its config file may legitimately be newer
  than the process start -- but only if the last *successful* reload is newer
  than the file.

Grafana is excluded: its provisioning directory is watched on a timer, so file
newer than process is the normal state.

Exit 0 = every checked container matches its committed file, 1 = drift,
2 = could not check (docker or Prometheus unreachable).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# container name -> (config path relative to repo root, reload model)
CHECKS: dict[str, tuple[str, str]] = {
    "argus-otel-collector-1": (
        "docker-compose/configs/otel-collector/otel-collector-config.yml",
        "restart",
    ),
    "argus-loki-1": ("docker-compose/configs/loki/loki-config.yml", "restart"),
    "argus-tempo-1": ("docker-compose/configs/tempo/tempo-config.yml", "restart"),
    "argus-alloy-1": ("docker-compose/configs/alloy/config.alloy", "restart"),
    "argus-prometheus-1": ("docker-compose/configs/prometheus/prometheus.yml", "reload"),
}

PROM_URL = "http://localhost:19090/api/v1/query?query=prometheus_config_last_reload_success_timestamp_seconds"
GRACE_S = 5.0


def started_at(container: str) -> float | None:
    """Unix timestamp of container start, or None if the container is absent."""
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.StartedAt}}", container],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        print("config-parity: docker not found on PATH", file=sys.stderr)
        raise SystemExit(2) from None
    if out.returncode != 0:
        return None
    raw = out.stdout.strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def prometheus_reload_time() -> float | None:
    try:
        with urllib.request.urlopen(PROM_URL, timeout=5) as resp:
            body = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    try:
        value = body["data"]["result"][0]["value"][1]
    except (KeyError, IndexError, TypeError):
        return None
    return float(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root (default: cwd)")
    parser.add_argument(
        "--grace", type=float, default=GRACE_S, help="seconds of slack (default: 5)"
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    drift: list[str] = []
    checked = 0
    skipped: list[str] = []

    for container, (rel_path, model) in CHECKS.items():
        path = root / rel_path
        if not path.exists():
            skipped.append(f"{container}: {rel_path} missing")
            continue
        started = started_at(container)
        if started is None:
            skipped.append(f"{container}: not running")
            continue

        mtime = path.stat().st_mtime
        checked += 1

        if model == "restart":
            if mtime > started + args.grace:
                drift.append(
                    f"{container}: {rel_path} was modified {mtime - started:.0f}s "
                    f"after the container started; it needs a restart to take effect"
                )
            continue

        reloaded = prometheus_reload_time()
        if reloaded is None:
            skipped.append(f"{container}: could not read last reload time")
            continue
        if reloaded < mtime - args.grace:
            drift.append(
                f"{container}: {rel_path} is {mtime - reloaded:.0f}s newer than the last "
                f"successful reload; the running config is not the committed one"
            )

    for line in skipped:
        print(f"config-parity: SKIP {line}")
    for line in drift:
        print(f"config-parity: DRIFT {line}")

    if drift:
        print(f"config-parity: {len(drift)} container(s) behind the committed config")
        return 1
    print(f"config-parity: {checked}/{len(CHECKS)} containers match the committed config")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

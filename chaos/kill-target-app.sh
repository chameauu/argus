#!/usr/bin/env bash
# Kill the workload and prove the availability alert fires, then recover it.
set -euo pipefail
cd "$(dirname "$0")/.."
. chaos/lib.sh

cleanup() { compose start target-app >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== stopping target-app =="
compose stop target-app >/dev/null

wait_alert TargetAppDown firing 180

echo "== starting target-app =="
compose start target-app >/dev/null

wait_alert TargetAppDown inactive 240
echo "PASS: a real outage raised and cleared TargetAppDown."

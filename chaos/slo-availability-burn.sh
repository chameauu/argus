#!/usr/bin/env bash
# Inject 5xx responses on /work and prove the availability burn-rate alert fires.
set -euo pipefail
cd "$(dirname "$0")/.."
. chaos/lib.sh

cleanup() { curl -sf -X POST "${TARGET_URL}/admin/fault/reset" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== injecting 95% failures on /work =="
curl -sf -X POST "${TARGET_URL}/admin/fault?error_rate=0.95" | jq -c .

wait_alert SLOAvailabilityBurnFast firing 480

echo "== clearing the fault =="
curl -sf -X POST "${TARGET_URL}/admin/fault/reset" | jq -c .

wait_sli 'slo:availability:ratio5m' '>=' 0.99 300
echo "PASS: injected failures burned the budget and the SLI recovered."
echo "      The alert stays pending until the 1h window drains — expected, not a bug."

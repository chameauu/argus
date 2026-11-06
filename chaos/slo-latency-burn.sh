#!/usr/bin/env bash
# Inject 500ms of delay into /work and prove the latency budget burns.
#
# This experiment asserts the fast-window burn condition the alert evaluates, not the alert
# state. With a 5% latency budget, a 14.4x burn means 72% of requests slow; the alert also
# requires the 1h window to agree, which needs about 43 minutes of sustained degradation when
# the hour still holds clean traffic. The availability experiment proves the alert transition;
# this one proves the latency SLI and the burn arithmetic behind it.
set -euo pipefail
cd "$(dirname "$0")/.."
. chaos/lib.sh

cleanup() { curl -sf -X POST "${TARGET_URL}/admin/fault/reset" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== injecting 500ms delay on /work =="
curl -sf -X POST "${TARGET_URL}/admin/fault?delay_seconds=0.5" | jq -c .

deadline=$(( $(date +%s) + 300 ))
burning=0
while [ "$(date +%s)" -lt "${deadline}" ]; do
  ratio=$(sli 'slo:latency:ratio5m')
  burn=$(awk -v r="${ratio}" 'BEGIN { printf "%.1f", (1 - r) / 0.05 }')
  echo "ratio5m=${ratio} burn=${burn}x"
  if awk -v b="${burn}" 'BEGIN { exit !(b > 14.4) }'; then burning=1; break; fi
  sleep 15
done
[ "${burning}" -eq 1 ] || { echo "FAIL: latency burn never exceeded 14.4x" >&2; exit 1; }

echo "== clearing the fault =="
curl -sf -X POST "${TARGET_URL}/admin/fault/reset" | jq -c .

wait_sli 'slo:latency:ratio5m' '>=' 0.95 300
echo "PASS: injected latency drove the fast-window burn above 14.4x and the SLI recovered."

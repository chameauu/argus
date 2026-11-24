#!/usr/bin/env bash
# Inject 5xx responses on /work and prove the generated availability burn-rate alert fires.
# The alert and the SLI both come from the Sloth-generated rules (alert-rules/generated/slo-*.yml);
# the hand-written slo-rules.yml they used to name was deleted in the generated-rules cutover.
set -euo pipefail
cd "$(dirname "$0")/.."
. chaos/lib.sh

cleanup() { curl -sf -X POST "${TARGET_URL}/admin/fault/reset" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== injecting 95% failures on /work =="
curl -sf -X POST "${TARGET_URL}/admin/fault?error_rate=0.95" | jq -c .

wait_alert TargetAppAvailabilityBurnRate firing 480

echo "== clearing the fault =="
curl -sf -X POST "${TARGET_URL}/admin/fault/reset" | jq -c .

wait_sli '1 - slo:sli_error:ratio_rate5m{sloth_service="target-app",sloth_slo="availability"}' '>=' 0.99 420
echo "PASS: injected failures burned the budget and the SLI recovered."
echo "      Recovery budget is 420s, not 300s: the SLI is a 5m window and one more scrape/eval"
echo "      cycle can sit exactly on a 300s boundary (measured: 0.938 at 300s, 1.0 shortly after)."
echo "      The two burn instances drain on their own windows — page ~1h, ticket ~6h — expected."

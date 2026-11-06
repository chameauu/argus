#!/usr/bin/env bash
# Run every chaos experiment in sequence. Takes roughly 15 minutes because the
# burn-rate alerts require real windows to fill; that is the point of the test.
set -euo pipefail
cd "$(dirname "$0")/.."

scripts=(
  chaos/queue-backlog.sh
  chaos/kill-target-app.sh
  chaos/slo-availability-burn.sh
  chaos/slo-latency-burn.sh
)

failed=0
for script in "${scripts[@]}"; do
  echo
  echo "===== ${script} ====="
  if bash "${script}"; then
    echo "result: PASS"
  else
    echo "result: FAIL" >&2
    failed=$((failed + 1))
  fi
done

echo
if [ "${failed}" -eq 0 ]; then
  echo "ALL CHAOS EXPERIMENTS PASSED"
else
  echo "${failed} chaos experiment(s) FAILED" >&2
  exit 1
fi

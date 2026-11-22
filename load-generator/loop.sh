#!/bin/sh
# Argus steady-state load generator.
#
# Default cycle (100 requests): 98x /work + 2x /error-prone.
#   - /error-prone fails ~15% of its own calls, so a 2% share blends to
#     ~0.30% 5xx at rest. That is well under the 1% RED threshold, the 5%
#     error-rate alert, and the 0.5% SLO burn ticket with real margin.
#   - /work is the only latency-carrying steady path (~90ms p99).
#
# Why /slow is not in the default cycle:
#   The generated RED rule computes p99 *per endpoint*
#   (histogram_quantile(0.99, sum by (le, endpoint) (...))), not blended.
#   /slow sleeps 0.6-1.2s on 10% of its own calls by design, so /slow's own
#   p99 is above 0.5s as soon as the 5m window contains a single slow call -
#   regardless of how small its share of total traffic is. Keeping it in the
#   steady mix therefore keeps TargetAppHighLatency firing/pending while the
#   blended p99 is perfectly healthy (~0.1s).
#
#   /slow remains available and is exercised on demand:
#     curl -s -o /dev/null http://localhost:18000/slow
#     ./k6/run.sh slow        # optional scenario, see k6/slow.js
#   Set LOAD_GENERATOR_SLOW_EVERY=N to put /slow back every Nth request
#   (e.g. 50 = 2% of the mix) when you deliberately want that rule to fire.
set -u

target="${TARGET_URL:-http://target-app:8000}"
pause="${LOAD_GENERATOR_PAUSE:-0.5}"
slow_every="${LOAD_GENERATOR_SLOW_EVERY:-0}"

i=0
while true; do
  i=$(( i % 100 + 1 ))
  if [ "${slow_every}" -gt 0 ] && [ $(( i % slow_every )) -eq 0 ]; then
    path="/slow"
  elif [ "$i" -eq 37 ] || [ "$i" -eq 73 ]; then
    path="/error-prone"
  else
    path="/work"
  fi
  curl -s -o /dev/null --max-time 5 "${target}${path}" || true
  sleep "$pause"
done

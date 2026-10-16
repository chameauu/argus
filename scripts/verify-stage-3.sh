#!/usr/bin/env bash
# Stage 3 acceptance check — traces pipeline.
# Written before the config (test-first): must FAIL before tempo/otel-collector
# exist, and PASS once target-app spans are queryable in Tempo.
#
# Usage: ./scripts/verify-stage-3.sh
set -euo pipefail

TEMPO="${TEMPO_URL:-http://localhost:13200}"
SERVICE="${SERVICE_NAME:-target-app}"
# Tempo search needs an explicit time window (unix seconds).
END=$(date +%s)
START=$((END - 3600))

echo "== 1. Tempo readiness =="
ready_code=$(curl -s -o /dev/null -w '%{http_code}' "${TEMPO}/ready")
echo "GET /ready -> ${ready_code}"
if [ "${ready_code}" != "200" ]; then
  echo "FAIL: Tempo not ready (http=${ready_code})" >&2
  exit 1
fi

echo
echo "== 2. Search traces for service.name=${SERVICE} =="
search=$(curl -sfG "${TEMPO}/api/search" \
  --data-urlencode "tags=service.name=${SERVICE}" \
  --data-urlencode "limit=20" \
  --data-urlencode "start=${START}" \
  --data-urlencode "end=${END}")

count=$(printf '%s' "${search}" | jq '.traces | length')
echo "traces=${count}"
printf '%s' "${search}" | jq -r '.traces[] | "\(.rootServiceName) \(.rootTraceName) \(.durationMs)ms"'

if [ "${count}" -lt 1 ]; then
  echo "FAIL: no traces found for service.name=${SERVICE}" >&2
  exit 1
fi

echo
echo "== 3. Verify a trace is retrievable by ID =="
trace_id=$(printf '%s' "${search}" | jq -r '.traces[0].traceID')
echo "traceID=${trace_id}"
spans=$(curl -sf "${TEMPO}/api/traces/${trace_id}" | jq '[.batches[].scopeSpans[].spans[]] | length')
echo "spans=${spans}"
if [ "${spans}" -lt 1 ]; then
  echo "FAIL: trace ${trace_id} has no retrievable spans" >&2
  exit 1
fi

echo
echo "PASS: traces are flowing into Tempo."

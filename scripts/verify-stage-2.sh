#!/usr/bin/env bash
# Stage 2 acceptance check — logs pipeline.
# Written before the config (test-first): must FAIL before loki/promtail exist,
# and PASS once container logs are queryable in Loki.
#
# Usage: ./scripts/verify-stage-2.sh
set -euo pipefail

LOKI="${LOKI_URL:-http://localhost:13100}"
STREAM_QUERY="${STREAM_QUERY:-{compose_service=\"target-app\"}}"

echo "== 1. Loki readiness =="
ready_code=$(curl -s -o /dev/null -w '%{http_code}' "${LOKI}/ready")
echo "GET /ready -> ${ready_code}"
if [ "${ready_code}" != "200" ]; then
  echo "FAIL: Loki not ready (http=${ready_code})" >&2
  exit 1
fi

echo
echo "== 2. Indexed label names (expect compose_service, container, ...) =="
curl -sf "${LOKI}/loki/api/v1/labels" | jq -r '.data[]'

echo
echo "== 3. Recent target-app log lines =="
curl -sfG "${LOKI}/loki/api/v1/query_range" \
  --data-urlencode "query=${STREAM_QUERY}" \
  --data-urlencode "limit=5" \
  | jq -r '.data.result[].values[][1]'

echo
echo "== 4. Stream count for target-app =="
count=$(curl -sfG "${LOKI}/loki/api/v1/query_range" \
  --data-urlencode "query=${STREAM_QUERY}" \
  --data-urlencode "limit=1" \
  | jq '.data.result | length')

echo "streams=${count}"
if [ "${count}" -lt 1 ]; then
  echo "FAIL: no log streams found for ${STREAM_QUERY}" >&2
  exit 1
fi

echo
echo "PASS: logs are flowing into Loki."

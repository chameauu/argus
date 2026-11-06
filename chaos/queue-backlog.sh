#!/usr/bin/env bash
# Force the business queue depth over its threshold and prove the alert fires.
set -euo pipefail
cd "$(dirname "$0")/.."
. chaos/lib.sh

exporter="${EXPORTER_URL:-http://localhost:19200}"

cleanup() { curl -sf -X POST "${exporter}/admin/reset" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== forcing job_queue_depth=250 =="
curl -sf -X POST "${exporter}/admin/override?job_queue_depth=250" | jq -c .

wait_alert CustomExporterQueueHigh firing 120

echo "== forcing the queue below threshold =="
curl -sf -X POST "${exporter}/admin/override?job_queue_depth=10" | jq -c .

wait_alert CustomExporterQueueHigh inactive 180

echo "== clearing the override =="
curl -sf -X POST "${exporter}/admin/reset" | jq -c .
echo "PASS: the queue backlog raised and cleared CustomExporterQueueHigh."

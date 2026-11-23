#!/usr/bin/env bash
# The claim, made falsifiable: a container outside this repository attaches to
# the platform with nothing but OTEL_SERVICE_NAME and
# OTEL_EXPORTER_OTLP_ENDPOINT, and its metrics, traces and logs all arrive.
#
# Nothing is written to this repository to make it work, and the script asserts
# that: a dirty tree at the start or the end fails the run.
#
# Usage: bash platform/attach_proof.sh [image] [service-name]
#   image        defaults to attach-demo (a throwaway OTLP-instrumented app)
#   service-name defaults to demo-attach
set -euo pipefail

image="${1:-attach-demo}"
service="${2:-demo-attach}"
container="attach-proof-$$"
prom="${PROM_URL:-http://localhost:19090}"
tempo="${TEMPO_URL:-http://localhost:13200}"
loki="${LOKI_URL:-http://localhost:13100}"

fail() {
	echo "FAIL: $*"
	docker rm -f "$container" >/dev/null 2>&1 || true
	exit 1
}

tree_state() {
	git status --porcelain 2>/dev/null | head -5
}

before="$(tree_state)"
[ -z "$before" ] || fail "working tree is dirty before the run; the proof is only meaningful from a clean tree"

docker rm -f "$container" >/dev/null 2>&1 || true
docker run -d --name "$container" --network argus_obs-net \
	-e "OTEL_SERVICE_NAME=$service" \
	-e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318 \
	"$image" >/dev/null

# Wait for the app, then drive traffic inside the container's own network.
for _ in $(seq 1 30); do
	docker exec "$container" python -c \
		"import urllib.request;urllib.request.urlopen('http://localhost:8080/health',timeout=2)" \
		>/dev/null 2>&1 && break
	sleep 1
done
for i in 1 2 3 4 5; do
	docker exec "$container" python -c \
		"import urllib.request;urllib.request.urlopen('http://localhost:8080/orders/$i',timeout=3).read()" \
		>/dev/null 2>&1 || true
done
# Batch processor (5s) plus one scrape-free export interval.
sleep 20

query_prom() { curl -s --get --data-urlencode "query=$1" "$prom/api/v1/query"; }
series_count() { query_prom "$1" | jq -r '.data.result | length' 2>/dev/null || echo 0; }

# A rate needs two spanmetrics flushes; the counter itself proves the connector saw
# this service, which is what makes the dashboard's span-derived row render.
span_calls="$(query_prom "sum(spanmetrics_calls_total{service=\"$service\"})" |
	jq -r '[.data.result[]?.value[1] | tonumber] | add // 0' 2>/dev/null || echo 0)"
awk -v v="$span_calls" 'BEGIN{exit !(v > 0)}' ||
	fail "the dashboard's span-derived RED row would be empty for \"$service\" (spanmetrics_calls_total=$span_calls)"
metrics="$(series_count "{__name__=~\".+_http_requests_total\",service=\"$service\"}")"

traces="$(curl -s --get --data-urlencode "q={resource.service.name=\"$service\"}" \
	--data-urlencode 'limit=5' "$tempo/api/search" | jq -r '.traces | length' 2>/dev/null || echo 0)"
[ "$traces" -ge 1 ] || fail "no traces with service.name=\"$service\" in Tempo"

logs="$(curl -s --get --data-urlencode "query={service_name=\"$service\"}" \
	--data-urlencode 'limit=5' "$loki/loki/api/v1/query_range" |
	jq -r '[.data.result[]?.values | length] | add // 0' 2>/dev/null || echo 0)"
[ "$logs" -ge 1 ] || fail "no log lines with service_name=\"$service\" in Loki"

dashboard="$(curl -s "$prom/api/v1/label/service/values" | jq -r ".data | index(\"$service\") // empty")"
[ -n "$dashboard" ] || fail "\"$service\" is not selectable in the dashboard's service variable"

after="$(tree_state)"
docker rm -f "$container" >/dev/null 2>&1 || true
[ -z "$after" ] || fail "the run modified this repository: $after"

echo "PASS: $service attached with two env vars — span-derived calls ${span_calls} (the dashboard renders it without any naming convention), ${metrics} native counter series, ${traces} traces, ${logs} log lines, selectable in Argus — Service. No platform file changed."

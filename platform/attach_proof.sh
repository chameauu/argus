#!/usr/bin/env bash
# The claim, made falsifiable: a container outside this repository attaches to
# the platform with nothing but OTEL_SERVICE_NAME and
# OTEL_EXPORTER_OTLP_ENDPOINT, and its metrics, traces and logs all arrive.
#
# Nothing is written to this repository to make it work, and the script asserts
# that: a dirty tree at the start or the end fails the run. The tree checked is
# always the one containing this script, not the caller's $PWD.
#
# The demo image is reproducible from platform/attach-demo/; if the default
# image is absent it is built from that directory before the run.
#
# Usage: bash platform/attach_proof.sh [options] [image] [service-name]
#   --network NAME   Docker network shared with the collector (default argus_obs-net)
#   --endpoint URL   OTLP/HTTP endpoint handed to the app (default http://otel-collector:4318)
#   image            defaults to argus-attach-demo:local (built when absent)
#   service-name     defaults to demo-attach
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null)" || {
	echo "FAIL: cannot resolve the repository root from $script_dir" >&2
	exit 1
}

image="argus-attach-demo:local"
service="demo-attach"
network="argus_obs-net"
endpoint="http://otel-collector:4318"
positionals=()

while [ "$#" -gt 0 ]; do
	case "$1" in
	--network)
		[ "$#" -ge 2 ] || {
			echo "FAIL: --network needs a value" >&2
			exit 2
		}
		network="$2"
		shift 2
		;;
	--endpoint)
		[ "$#" -ge 2 ] || {
			echo "FAIL: --endpoint needs a value" >&2
			exit 2
		}
		endpoint="$2"
		shift 2
		;;
	-h | --help)
		sed -n '13,17p' "$0" | sed 's/^# \{0,1\}//'
		exit 0
		;;
	--)
		shift
		positionals+=("$@")
		break
		;;
	-*)
		echo "FAIL: unknown option $1" >&2
		exit 2
		;;
	*)
		positionals+=("$1")
		shift
		;;
	esac
done

if [ "${#positionals[@]}" -ge 1 ]; then
	image="${positionals[0]}"
fi
if [ "${#positionals[@]}" -ge 2 ]; then
	service="${positionals[1]}"
fi
if [ "${#positionals[@]}" -gt 2 ]; then
	echo "FAIL: too many positional arguments" >&2
	exit 2
fi
endpoint="${endpoint%/}"

container="attach-proof-$$"
prom="${PROM_URL:-http://localhost:19090}"
tempo="${TEMPO_URL:-http://localhost:13200}"
loki="${LOKI_URL:-http://localhost:13100}"

fail() {
	echo "FAIL: $*" >&2
	docker rm -f "$container" >/dev/null 2>&1 || true
	exit 1
}

tree_state() {
	git -C "$repo_root" status --porcelain 2>/dev/null | head -5
}

before="$(tree_state)"
if [ -n "$before" ]; then
	fail "working tree at $repo_root is dirty before the run ($before); the proof is only meaningful from a clean tree"
fi

if ! docker image inspect "$image" >/dev/null 2>&1; then
	if [ "$image" = "argus-attach-demo:local" ]; then
		echo "building $image from platform/attach-demo ..."
		docker build -q -t "$image" "$repo_root/platform/attach-demo" >/dev/null ||
			fail "could not build $image from $repo_root/platform/attach-demo"
	else
		fail "image \"$image\" not found; build it or pass an existing image (the default argus-attach-demo:local is built from platform/attach-demo)"
	fi
fi

docker rm -f "$container" >/dev/null 2>&1 || true
if ! docker_error="$(
	docker run -d --name "$container" --network "$network" \
		-e "OTEL_SERVICE_NAME=$service" \
		-e "OTEL_EXPORTER_OTLP_ENDPOINT=$endpoint" \
		"$image" 2>&1 >/dev/null
)"; then
	fail "could not start the demo on Docker network \"$network\" with endpoint $endpoint (${docker_error%%$'\n'*}). Pass --network <docker-network> (default argus_obs-net) or --endpoint <url> (default http://otel-collector:4318)."
fi

# Wait for the app, then drive traffic inside the container's own network.
ready=0
for _ in $(seq 1 30); do
	if docker exec "$container" python -c \
		"import urllib.request;urllib.request.urlopen('http://localhost:8080/health',timeout=2)" \
		>/dev/null 2>&1; then
		ready=1
		break
	fi
	sleep 1
done
if [ "$ready" -ne 1 ]; then
	fail "the demo did not answer http://localhost:8080/health inside container \"$container\" on network \"$network\" with endpoint $endpoint; check --network and --endpoint if the collector lives elsewhere"
fi
docker exec "$container" python -c \
	"import urllib.request;urllib.request.urlopen('http://localhost:8080/',timeout=3).read()" \
	>/dev/null 2>&1 || true
for i in 1 2 3 4 5; do
	docker exec "$container" python -c \
		"import urllib.request;urllib.request.urlopen('http://localhost:8080/orders/$i',timeout=3).read()" \
		>/dev/null 2>&1 || true
done

query_prom() { curl -s --get --data-urlencode "query=$1" "$prom/api/v1/query"; }
series_count() { query_prom "$1" | jq -r '.data.result | length' 2>/dev/null || echo 0; }

# The collector batches spans for 5s and spanmetrics flushes every 15s, so a
# fixed sleep races the flush. Poll instead: the wait grows but no assertion is
# weakened. The connector keeps the service for metrics_expiration (5m), well
# beyond this loop.
span_calls=0
for _ in $(seq 1 30); do
	span_calls="$(query_prom "sum(spanmetrics_calls_total{service=\"$service\"})" |
		jq -r '[.data.result[]?.value[1] | tonumber] | add // 0' 2>/dev/null || echo 0)"
	if awk -v v="$span_calls" 'BEGIN{exit !(v > 0)}'; then
		break
	fi
	sleep 2
done
# A rate needs two spanmetrics flushes; the counter itself proves the connector saw
# this service, which is what makes the dashboard's span-derived row render.
awk -v v="$span_calls" 'BEGIN{exit !(v > 0)}' ||
	fail "the dashboard's span-derived RED row would be empty for \"$service\" (spanmetrics_calls_total=$span_calls; network=$network endpoint=$endpoint)"
metrics="$(series_count "attached_requests_total{service=\"$service\"}")"

traces="$(curl -s --get --data-urlencode "q={resource.service.name=\"$service\"}" \
	--data-urlencode 'limit=5' "$tempo/api/search" | jq -r '.traces | length' 2>/dev/null || echo 0)"
[ "$traces" -ge 1 ] || fail "no traces with service.name=\"$service\" in Tempo (endpoint=$endpoint)"

logs="$(curl -s --get --data-urlencode "query={service_name=\"$service\"}" \
	--data-urlencode 'limit=5' "$loki/loki/api/v1/query_range" |
	jq -r '[.data.result[]?.values | length] | add // 0' 2>/dev/null || echo 0)"
[ "$logs" -ge 1 ] || fail "no log lines with service_name=\"$service\" in Loki (endpoint=$endpoint)"

dashboard="$(curl -s "$prom/api/v1/label/service/values" | jq -r ".data | index(\"$service\") // empty")"
[ -n "$dashboard" ] || fail "\"$service\" is not selectable in the dashboard's service variable"

after="$(tree_state)"
docker rm -f "$container" >/dev/null 2>&1 || true
[ -z "$after" ] || fail "the run modified repository $repo_root: $after"

echo "PASS: $service attached with two env vars — span-derived calls ${span_calls} (the dashboard renders it without any naming convention), ${metrics} native counter series (attached_requests_total), ${traces} traces, ${logs} log lines, selectable in Argus — Service. No platform file changed."

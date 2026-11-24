#!/usr/bin/env bash
# Start the two-app example and prove both integration tiers against the live
# platform. Leaves postgres, postgres-exporter and backend running.
#
# Usage: bash examples/backend-with-postgres/run.sh
#
# What it changes in the argus repository (all reverted by stop.sh):
#   services/demo-postgres.yaml       installed from this example
#   alert-rules/generated/red-demo-postgres.yml
#   runbooks/generated/demo-postgres.md
#   docker-compose/configs/prometheus/prometheus.yml (one scrape job added)
# Nothing under platform/, dashboards/ or docker-compose/ is hand-edited.
set -euo pipefail

example_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(git -C "$example_dir" rev-parse --show-toplevel 2>/dev/null)" || {
	echo "FAIL: cannot resolve the argus repository root from $example_dir" >&2
	exit 1
}

network="argus_obs-net"
prom="${ARGUS_PROMETHEUS_URL:-http://localhost:19090}"
tempo="${ARGUS_TEMPO_URL:-http://localhost:13200}"
backend_port="${DEMO_BACKEND_PORT:-18001}"
backend="http://localhost:${backend_port}"
descriptor_src="$example_dir/services/demo-postgres.yaml"
descriptor_dst="$repo_root/services/demo-postgres.yaml"

fail() {
	echo "FAIL: $*" >&2
	exit 1
}

compose() {
	docker compose -f "$example_dir/docker-compose.yml" "$@"
}

prom_query() {
	curl -s --get --data-urlencode "query=$1" "$prom/api/v1/query"
}

prom_scalar() {
	prom_query "$1" | jq -r '[.data.result[]?.value[1] | tonumber] | add // 0'
}

echo "== preconditions =="
docker network inspect "$network" >/dev/null 2>&1 ||
	fail "Docker network $network does not exist; start the argus stack first"
curl -sf "$prom/-/ready" >/dev/null || fail "Prometheus is not ready at $prom"
curl -sf "$backend/health" >/dev/null 2>&1 && fail "something already answers $backend/health"
echo "network $network present; Prometheus ready at $prom; $backend is free"

echo
echo "== install the Tier B descriptor (nothing in platform/ is edited) =="
if [ -e "$descriptor_dst" ] && ! cmp -s "$descriptor_src" "$descriptor_dst"; then
	fail "$descriptor_dst already exists with different contents; refusing to overwrite it"
fi
install -m 0644 "$descriptor_src" "$descriptor_dst"
echo "installed services/demo-postgres.yaml"

echo
echo "== render and reload =="
(
	cd "$repo_root"
	python3 platform/generate.py
)
prom_container="$(docker ps \
	--filter label=com.docker.compose.project=argus \
	--filter label=com.docker.compose.service=prometheus \
	--format '{{.Names}}' | head -1)"
[ -n "$prom_container" ] || fail "could not find the argus Prometheus container"
config_mtime="$(stat -c %Y "$repo_root/docker-compose/configs/prometheus/prometheus.yml")"
docker kill -s HUP "$prom_container" >/dev/null
# Wait for the reload to actually complete: `last_reload_successful` can still
# read the previous (successful) reload for a moment, which is enough for an
# immediate config_parity run to report drift.
reloaded=0
for _ in $(seq 1 30); do
	reload_ts="$(prom_query 'prometheus_config_last_reload_success_timestamp_seconds' |
		jq -r '.data.result[0].value[1] // 0')"
	reload_ok="$(prom_query 'prometheus_config_last_reload_successful' |
		jq -r '.data.result[0].value[1] // 0')"
	if [ "$reload_ok" = "1" ] &&
		awk -v ts="$reload_ts" -v m="$config_mtime" 'BEGIN{exit !(ts >= m)}'; then
		reloaded=1
		break
	fi
	sleep 1
done
[ "$reloaded" -eq 1 ] || fail "Prometheus did not reload the new scrape config within 30s"
echo "Prometheus reloaded (last reload successful=$reload_ok, at $reload_ts)"

echo
echo "== docker compose up -d --build =="
compose up -d --build

echo
echo "== wait for health =="
deadline=$((SECONDS + 120))
while :; do
	ids="$(compose ps -q)"
	count="$(printf '%s\n' "$ids" | grep -c . || true)"
	healthy=1
	detail=""
	for id in $ids; do
		state="$(docker inspect -f '{{.State.Status}}' "$id")"
		health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$id")"
		name="$(docker inspect -f '{{.Name}}' "$id" | sed 's#^/##')"
		detail+="  $name $state ${health:-(no healthcheck)}"$'\n'
		[ "$state" = "running" ] || healthy=0
		if [ -n "$health" ] && [ "$health" != "healthy" ]; then healthy=0; fi
	done
	if [ "$count" -eq 3 ] && [ "$healthy" -eq 1 ] && curl -sf "$backend/health" >/dev/null; then
		printf '%s' "$detail"
		echo "all three containers running; backend /health answers"
		break
	fi
	if [ "$SECONDS" -ge "$deadline" ]; then
		printf '%s' "$detail"
		compose logs --tail=50
		fail "containers did not become healthy within 120s"
	fi
	sleep 2
done

echo
echo "== drive traffic =="
for _ in $(seq 1 5); do curl -sf "$backend/orders" >/dev/null; done
for _ in $(seq 1 5); do curl -sf "$backend/orders/1" >/dev/null; done
for i in 1 2; do
	curl -sf -X POST -H 'Content-Type: application/json' \
		-d "{\"item\":\"demo-$i\"}" "$backend/orders" >/dev/null
done
echo "GET /orders x5, GET /orders/1 x5, POST /orders x2"
echo "GET /orders -> $(curl -s "$backend/orders" | jq -c .)"

echo
echo "== wait up to 60s for the platform to show the backend (Tier A) =="
calls=0
for _ in $(seq 1 30); do
	calls="$(prom_scalar 'sum(spanmetrics_calls_total{service="demo-backend"})')"
	awk -v v="$calls" 'BEGIN{exit !(v > 0)}' && break
	sleep 2
done
awk -v v="$calls" 'BEGIN{exit !(v > 0)}' ||
	fail "demo-backend never appeared in spanmetrics_calls_total (got $calls); check the collector endpoint and network"

service_label="$(curl -s "$prom/api/v1/label/service/values" |
	jq -r 'if (.data | index("demo-backend")) != null then "present" else "missing" end')"
traces="$(curl -s --get --data-urlencode 'q={resource.service.name="demo-backend"}' \
	--data-urlencode 'limit=5' "$tempo/api/search" | jq -r '.traces | length' 2>/dev/null || echo 0)"

echo
echo "== wait up to 60s for the database scrape (Tier B) =="
pg_up=0
up=0
pg_series=0
for _ in $(seq 1 30); do
	up="$(prom_scalar 'up{job="demo-postgres"}')"
	pg_up="$(prom_scalar 'pg_up{service="demo-postgres"}')"
	pg_series="$(prom_scalar 'count({service="demo-postgres"})')"
	if [ "$up" = "1" ] && [ "$pg_up" = "1" ]; then break; fi
	sleep 2
done

echo
echo "== verification results =="
echo "Tier A  spanmetrics_calls_total{service=\"demo-backend\"} = $calls"
echo "Tier A  /api/v1/label/service/values contains demo-backend: $service_label"
echo "Tier A  Tempo traces for service.name=demo-backend (last 15m): $traces"
echo "Tier B  up{job=\"demo-postgres\"} = $up"
echo "Tier B  pg_up{service=\"demo-postgres\"} = $pg_up"
echo "Tier B  count({service=\"demo-postgres\"}) = $pg_series"

[ "$up" = "1" ] || fail "demo-postgres scrape target is not up"
[ "$pg_up" = "1" ] || fail "postgres_exporter cannot reach the database (pg_up=$pg_up)"
[ "$pg_series" -gt 0 ] || fail "no series carry service=\"demo-postgres\""
[ "$service_label" = "present" ] || fail "demo-backend is not selectable in the service label values"
[ "$traces" -ge 1 ] || fail "no demo-backend traces in Tempo"

echo
echo "PASS: both apps are integrated. Backend attached with env vars only (no descriptor);"
echo "      PostgreSQL is scraped through its exporter and described by services/demo-postgres.yaml."
echo "      Containers left running. Revert everything with: bash $example_dir/stop.sh"

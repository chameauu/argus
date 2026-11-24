#!/usr/bin/env bash
# Tear down the two-app example and put the argus repository back exactly as it
# was: remove the descriptor installed by run.sh, prune the generated artefacts
# and reload Prometheus. Safe to run twice.
#
# Usage: bash examples/backend-with-postgres/stop.sh
set -euo pipefail

example_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(git -C "$example_dir" rev-parse --show-toplevel 2>/dev/null)" || {
	echo "FAIL: cannot resolve the argus repository root from $example_dir" >&2
	exit 1
}

descriptor_src="$example_dir/services/demo-postgres.yaml"
descriptor_dst="$repo_root/services/demo-postgres.yaml"

fail() {
	echo "FAIL: $*" >&2
	exit 1
}

echo "== docker compose down -v (project demo-pair) =="
docker compose -f "$example_dir/docker-compose.yml" down -v

remaining="$(docker ps -aq --filter label=com.docker.compose.project=demo-pair)"
if [ -n "$remaining" ]; then
	docker ps -a --filter label=com.docker.compose.project=demo-pair
	fail "demo-pair containers remain after down"
fi
echo "no demo-pair containers remain"

echo
echo "== remove the descriptor and prune its generated artefacts =="
if [ -e "$descriptor_dst" ]; then
	if cmp -s "$descriptor_src" "$descriptor_dst"; then
		rm -f "$descriptor_dst"
		echo "removed services/demo-postgres.yaml"
	else
		fail "$descriptor_dst differs from the example; refusing to delete a file this script did not install"
	fi
else
	echo "services/demo-postgres.yaml is already absent"
fi

(
	cd "$repo_root"
	python3 platform/generate.py
	# Generated files are tracked; restore their exact committed bytes even if a
	# previous interrupted run left them modified. Only generated paths, never
	# hand-written rules.
	if git rev-parse --git-dir >/dev/null 2>&1; then
		git checkout -- \
			docker-compose/configs/prometheus/prometheus.yml \
			alert-rules/generated \
			runbooks/generated
	fi
)

prom_container="$(docker ps \
	--filter label=com.docker.compose.project=argus \
	--filter label=com.docker.compose.service=prometheus \
	--format '{{.Names}}' | head -1)"
if [ -n "$prom_container" ]; then
	config_mtime="$(stat -c %Y "$repo_root/docker-compose/configs/prometheus/prometheus.yml")"
	docker kill -s HUP "$prom_container" >/dev/null
	# The reload is asynchronous. Wait until the success timestamp is newer than
	# the config file, otherwise an immediate config_parity run reports drift.
	reload_ts=0
	for _ in $(seq 1 30); do
		reload_ts="$(curl -s --get \
			--data-urlencode 'query=prometheus_config_last_reload_success_timestamp_seconds' \
			"${ARGUS_PROMETHEUS_URL:-http://localhost:19090}/api/v1/query" |
			jq -r '.data.result[0].value[1] // 0' 2>/dev/null || echo 0)"
		if awk -v ts="$reload_ts" -v m="$config_mtime" 'BEGIN{exit !(ts >= m)}'; then
			break
		fi
		sleep 1
	done
	echo "Prometheus reloaded without the demo-postgres job (reload ts $reload_ts)"
fi

echo
echo "STOPPED: demo-pair containers and volume removed; argus generated files restored."

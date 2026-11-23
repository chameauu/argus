#!/usr/bin/env bash
# Live contract gate used by the pre-commit hook.
#
# Derives the --service list from services/*.yaml, exactly as the CI step in
# .github/workflows/ci.yml does: descriptor name -> scrape.port -> published
# host port via `docker compose port`. Adding a descriptor therefore needs no
# edit here or in .pre-commit-config.yaml.
#
# Needs the demo stack up:
#   docker compose -f docker-compose/docker-compose.yml up -d --build
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

compose="docker-compose/docker-compose.yml"
prometheus="${ARGUS_PROMETHEUS_URL:-http://localhost:19090}"

args=()
for descriptor in services/*.yaml; do
  name="$(basename "$descriptor" .yaml)"
  port="$(python3 -c 'import sys; sys.path.insert(0, "platform"); from descriptor import parse_descriptor; print(parse_descriptor(open(sys.argv[1], encoding="utf-8").read())["scrape"]["port"])' "$descriptor")"
  host_port="$(docker compose -f "$compose" port "$name" "$port" 2>/dev/null | awk -F: 'END {print $NF}')" || true
  if [ -z "$host_port" ]; then
    echo "no published host port for $name (scrape.port $port); is it in $compose?" >&2
    exit 1
  fi
  args+=("--service" "$name=http://localhost:$host_port")
done

printf 'contract targets:'
printf ' %s' "${args[@]}"
echo

# Gate 0: the running containers must be running the committed config files.
# Without this, a check can pass against a container that is serving a config
# nobody reviewed, which is how spanmetrics lived only in memory for an hour.
python3 platform/config_parity.py

exec python3 platform/contract_live.py "${args[@]}" --prometheus "$prometheus"

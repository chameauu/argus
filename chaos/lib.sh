#!/usr/bin/env bash
# Shared helpers for the chaos scripts. Each script asserts that an injected
# fault produces the expected alert, then clears the fault.
PROM_URL="${PROM_URL:-http://localhost:19090}"
TARGET_URL="${TARGET_URL:-http://localhost:18000}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose/docker-compose.yml}"

compose() { docker compose -f "${COMPOSE_FILE}" "$@"; }

alert_state() {
  curl -sf "${PROM_URL}/api/v1/rules?type=alert" \
    | jq -r --arg name "$1" '.data.groups[].rules[] | select(.name==$name) | .state' \
    | head -1
}

wait_alert() {
  local name="$1" want="$2" timeout="${3:-240}" deadline state=""
  deadline=$(( $(date +%s) + timeout ))
  while [ "$(date +%s)" -lt "${deadline}" ]; do
    state=$(alert_state "${name}")
    if [ "${state}" = "${want}" ]; then
      echo "${name} is ${want}"
      return 0
    fi
    sleep 10
  done
  echo "FAIL: ${name} did not reach ${want} within ${timeout}s (last: ${state:-none})" >&2
  return 1
}

sli() {
  curl -sf --get --data-urlencode "query=$1" "${PROM_URL}/api/v1/query" \
    | jq -r '.data.result[0].value[1] // "0"'
}

wait_sli() {
  local query="$1" op="$2" threshold="$3" timeout="${4:-300}" deadline value=""
  deadline=$(( $(date +%s) + timeout ))
  while [ "$(date +%s)" -lt "${deadline}" ]; do
    value=$(sli "${query}")
    if awk -v v="${value}" -v t="${threshold}" -v o="${op}" \
        'BEGIN { if (o == ">=") exit !(v >= t); else exit !(v <= t) }'; then
      echo "${query} = ${value}"
      return 0
    fi
    sleep 10
  done
  echo "FAIL: ${query} did not go ${op} ${threshold} within ${timeout}s (last: ${value})" >&2
  return 1
}

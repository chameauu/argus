#!/usr/bin/env bash
# Run one k6 scenario inside the Compose network so it reaches target-app by name.
# Usage: ./k6/run.sh [smoke|steady|spike|soak]
set -euo pipefail

scenario="${1:-smoke}"
network="${K6_NETWORK:-argus_obs-net}"
image="${K6_IMAGE:-grafana/k6:0.53.0}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ -f "${here}/${scenario}.js" ] || { echo "unknown scenario: ${scenario}" >&2; exit 1; }

docker run --rm -i \
  --network "${network}" \
  -e TARGET_URL="${TARGET_URL:-http://target-app:8000}" \
  -v "${here}:/scripts:ro" \
  "${image}" run "/scripts/${scenario}.js"

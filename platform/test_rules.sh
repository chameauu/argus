#!/usr/bin/env bash
# Run the rule gates: the stdlib golden tests (route-regex escaping and the
# scrape-time cardinality guard), then the generated rule unit tests with
# `promtool test rules`.
#
# The promtool tests live in platform/tests/*.yml and reference
# ../../alert-rules/generated relative to the test file. The whole repository is
# mounted read-only at /workspace so that resolution works unchanged inside the
# pinned Prometheus image. This is an offline gate: it needs a container
# runtime, not the stack.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="${PROMTOOL_IMAGE:-prom/prometheus:v2.53.0}"

# Golden tests for the generator run first: they are stdlib-only and catch the
# `{id}` escaping regression, any drift in the labelkeep allowlist, and a
# metric-less descriptor quietly growing a Sloth SLO file.
python3 "$repo_root/platform/tests/test_routes_regex.py"
python3 "$repo_root/platform/tests/test_cardinality_guard.py"
python3 "$repo_root/platform/tests/test_minimal_descriptor.py"

shopt -s nullglob
tests=("$repo_root"/platform/tests/*.yml)
if [ ${#tests[@]} -eq 0 ]; then
  echo "test_rules: no tests found in platform/tests" >&2
  exit 1
fi

targets=()
for test_file in "${tests[@]}"; do
  targets+=("/workspace/${test_file#"$repo_root"/}")
done

exec docker run --rm --entrypoint promtool \
  -v "$repo_root:/workspace:ro" \
  "$image" test rules "${targets[@]}"

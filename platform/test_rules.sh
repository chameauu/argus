#!/usr/bin/env bash
# Run the generated rule unit tests with `promtool test rules`.
#
# The tests live in platform/tests/*.yml and reference ../../alert-rules/generated
# relative to the test file. The whole repository is mounted read-only at
# /workspace so that resolution works unchanged inside the pinned Prometheus
# image. This is an offline gate: it needs a container runtime, not the stack.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="${PROMTOOL_IMAGE:-prom/prometheus:v2.53.0}"

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

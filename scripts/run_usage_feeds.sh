#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$script_dir/.."
# No runtime dependencies: use the stable interpreter name, not a versioned venv.
python_bin="${PYTHON:-python3}"
collect_status=0
"$python_bin" -m trmnl_agent_usage.collect_usage_feeds || collect_status=$?
# A failed provider writes an error feed; still deliver both providers' results.
"$python_bin" -m trmnl_agent_usage.push_usage_feeds
exit "$collect_status"

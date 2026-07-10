#!/usr/bin/env bash
# Fetch GitHub Actions logs for a run, pre-filtered so only relevant lines enter context.
# Usage: scripts/logs.sh <run-id> [grep-pattern]
#   no pattern -> only failed-step logs (tail-limited); with pattern -> grep the full log.
set -uo pipefail
run_id="${1:?usage: scripts/logs.sh <run-id> [grep-pattern]}"
pattern="${2:-}"
if [ -n "$pattern" ]; then
  gh run view "$run_id" --log 2>/dev/null | grep -E "$pattern" | tail -100
else
  gh run view "$run_id" --log-failed 2>/dev/null | tail -100
fi

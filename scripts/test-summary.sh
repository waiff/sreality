#!/usr/bin/env bash
# Quiet pytest; on success print the summary line, on failure only the failing tests + last
# ~40 lines. The default test helper (CLAUDE.md "How to test changes", scraper-ops skill).
# Usage: scripts/test-summary.sh [pytest args]
set -uo pipefail
out="$(python3 -m pytest -q "$@" 2>&1)"
status=$?
if [ "$status" -eq 0 ]; then
  printf '%s\n' "$out" | tail -1
else
  printf '%s\n' "$out" | grep -E '^(FAILED|ERROR|E  )' || true
  echo "----- last 40 lines -----"
  printf '%s\n' "$out" | tail -40
fi
exit "$status"

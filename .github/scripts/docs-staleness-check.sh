#!/usr/bin/env bash
# Non-blocking staleness nudge: this is the anti-recurrence mechanism, not a gate.
# Warns (GitHub warning annotation, never fails the build) when a PR changes code a
# skill documents but touches no file under that skill's directory. Plenty of PRs
# legitimately need no skill change — a check that cries wolf gets ignored or disabled,
# so this only ever warns, making "did the skill move too?" a visible question.
#
# Usage: .github/scripts/docs-staleness-check.sh <base-ref> [head-ref]
set -uo pipefail

BASE="${1:?usage: docs-staleness-check.sh <base-ref> [head-ref]}"
HEAD="${2:-HEAD}"

changed=$(git diff --name-only "${BASE}...${HEAD}" 2>/dev/null || git diff --name-only "${BASE}" "${HEAD}")
if [[ -z "$changed" ]]; then
  exit 0
fi

skill_touched() {
  local skill="$1"
  grep -q "^\.claude/skills/${skill}/" <<< "$changed"
}

warn_if_stale() {
  local pattern="$1" skill="$2" label="$3"
  if grep -qE "$pattern" <<< "$changed" && ! skill_touched "$skill"; then
    echo "::warning::This PR changes ${label} but doesn't touch .claude/skills/${skill}/SKILL.md — confirm the skill still describes current behavior (or that this change genuinely doesn't need it). See CLAUDE.md's Git-workflow rule: update the owning skill in the same PR."
  fi
}

warn_if_stale '^migrations/'                                    database       "migrations/"
warn_if_stale '^(toolkit|api)/'                                  toolkit-api    "toolkit/ or api/"
warn_if_stale '^api/llm_client\.py$|^api/providers/'             llm-pipelines  "the LLM provider/client layer"
warn_if_stale '^scraper/|^\.github/workflows/.*(index_walk|detail_drain)' scraper-ops "scraper/ or an index-walk/detail-drain workflow"
warn_if_stale '^frontend/'                                       interface-design "frontend/"

exit 0

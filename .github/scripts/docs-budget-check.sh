#!/usr/bin/env bash
# Hard-fail context-budget gate for always-loaded docs (CLAUDE.md's Context-discipline
# rule). Every line here is paid for on every request in every session, so budgets are
# enforced in CI instead of relying on review discipline.
#
# Usage: .github/scripts/docs-budget-check.sh [repo-root]
set -uo pipefail

ROOT="${1:-.}"
cd "$ROOT" || exit 1

fail=0

line_count() { wc -l < "$1" | tr -d ' '; }

check_lines() {
  local file="$1" max="$2" label="$3"
  [[ -f "$file" ]] || return 0
  local n
  n=$(line_count "$file")
  if (( n > max )); then
    echo "::error file=${file}::${label} is ${n}/${max} lines. Always-loaded content is paid for on every request in every session — move the detail into the owning skill (or that skill's references/) and leave a one-line pointer here. See \"Where the detail lives\" in CLAUDE.md."
    fail=1
  else
    echo "OK   ${label}: ${n}/${max} lines (${file})"
  fi
}

check_lines "CLAUDE.md" 300 "CLAUDE.md"
check_lines "ROADMAP.md" 120 "ROADMAP.md"

for skill_file in .claude/skills/*/SKILL.md; do
  [[ -f "$skill_file" ]] || continue
  check_lines "$skill_file" 500 "skill body (${skill_file})"
done

# Frontmatter `description:` is always loaded into every session (not just when the
# skill fires), so it gets its own budget separate from the body.
for skill_file in .claude/skills/*/SKILL.md; do
  [[ -f "$skill_file" ]] || continue
  desc_line=$(grep -m1 '^description:' "$skill_file" || true)
  if [[ -z "$desc_line" ]]; then
    echo "::error file=${skill_file}::skill frontmatter has no 'description:' line."
    fail=1
    continue
  fi
  desc="${desc_line#description:}"
  words=$(wc -w <<< "$desc" | tr -d ' ')
  if (( words > 150 )); then
    echo "::error file=${skill_file}::frontmatter description is ${words}/~150 words. Descriptions are always-loaded into every session (unlike the body, which loads on demand) — trim it and move detail into the skill body instead."
    fail=1
  else
    echo "OK   ${skill_file} description: ${words}/~150 words"
  fi
done

# Routing completeness: a skill nobody routes to never fires. Every directory under
# .claude/skills/ must be named in CLAUDE.md's "Where the detail lives" table.
if [[ -f CLAUDE.md ]]; then
  for skill_dir in .claude/skills/*/; do
    [[ -d "$skill_dir" ]] || continue
    name=$(basename "$skill_dir")
    if ! grep -q "\.claude/skills/${name}\b" CLAUDE.md; then
      echo "::error file=CLAUDE.md::skill '${name}' exists under .claude/skills/ but isn't referenced in the \"Where the detail lives\" table, so a session doing that kind of work never learns it exists. Add a row: | <when to load> | \`.claude/skills/${name}\` |"
      fail=1
    else
      echo "OK   routing: ${name} referenced in CLAUDE.md"
    fi
  done
fi

echo
if (( fail )); then
  echo "Docs budget check FAILED."
  exit 1
fi
echo "Docs budget check passed."

#!/usr/bin/env bash
# lint-context-budget.sh — enforce token budgets on always-loaded context files.
# Every line of CLAUDE.md/AGENTS.md is re-sent to the agent each turn, and every
# line of a SKILL.md is paid on each skill load — growth here is a per-turn token
# tax (PI-641). Official guidance: agent instruction files < 200 lines, skill
# bodies < 500 lines. Exit 0 on clean, exit 1 with actionable messages.
#
# Thresholds are overridable for projects with a deliberate different budget:
#   CONTEXT_BUDGET_LINES (default 200) — root CLAUDE.md / AGENTS.md
#   SKILL_BUDGET_LINES   (default 500) — .agents/skills/*/SKILL.md

case "${1-}" in
-h | --help) # the header above is the help; nothing else runs (#992)
  sed -n '2,/^[^#]/s/^# \{0,1\}//p' "$0"
  exit 0
  ;;
esac
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

CONTEXT_BUDGET_LINES="${CONTEXT_BUDGET_LINES:-200}"
SKILL_BUDGET_LINES="${SKILL_BUDGET_LINES:-500}"

ERRORS=0

check_budget() {
  file="$1"
  cap="$2"
  [ -f "$file" ] || return 0
  lines="$(wc -l <"$file" | tr -d '[:space:]')"
  if [ "$lines" -gt "$cap" ]; then
    echo "ERROR: ${file#"$ROOT"/}: $lines lines (budget: $cap) — trim it. Move reference material to .agents/docs/guides/ and keep only per-turn rules (see the token-efficiency skill)." >&2
    ERRORS=$((ERRORS + 1))
  fi
}

# Always-loaded tier: the agent instruction files at the repo root.
check_budget "$ROOT/CLAUDE.md" "$CONTEXT_BUDGET_LINES"
check_budget "$ROOT/AGENTS.md" "$CONTEXT_BUDGET_LINES"

# Load-on-invoke tier: skill bodies. `.agents/` is the authored source; the
# `.claude/` mirror and per-surface copies are generated from it, so linting
# the source covers them. The glob simply matches nothing if skills are absent.
for skill in "$ROOT"/.agents/skills/*/SKILL.md; do
  check_budget "$skill" "$SKILL_BUDGET_LINES"
done

if [ "$ERRORS" -gt 0 ]; then
  echo "lint_context_budget: $ERRORS file(s) over budget" >&2
  exit 1
fi

exit 0

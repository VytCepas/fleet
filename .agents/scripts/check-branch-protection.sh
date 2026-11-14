#!/usr/bin/env bash
# Diagnose the silent forever-block (PI-819): a REQUIRED status check that no
# workflow job ever reports.
#
# Branch protection is written once, at scaffold time, by setup-github.sh. The
# workflows keep evolving — jobs get renamed, a matrix stops fanning out per-PR
# (PI-761). A required context that no job produces is never reported, so it stays
# pending forever: `mergeStateStatus` is BLOCKED, every check is green, no error is
# printed anywhere, and EVERY pull request in the repo becomes unmergeable.
#
# There is nothing to see in the CI logs, because CI passed. This script names the
# phantom contexts and the fix.
#
# Usage: check-branch-protection.sh [PR_NUMBER]
#   Defaults to the PR for the current branch. A PR is required: see below.
# Exit 0 = protection is satisfiable; 1 = phantom contexts found; 0 also when the
# state cannot be determined (no gh, not authed, no protection) — a diagnostic must
# never itself block a workflow.

case "${1-}" in
-h | --help) # the header above is the help; nothing else runs (#992)
  sed -n '2,/^[^#]/s/^# \{0,1\}//p' "$0"
  exit 0
  ;;
esac
set -euo pipefail

PR_NUMBER="${1:-}"

if ! command -v gh >/dev/null 2>&1; then
  echo "check_branch_protection: gh not installed — skipping (diagnostic only)." >&2
  exit 0
fi

REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)
if [ -z "$REPO" ]; then
  echo "check_branch_protection: no GitHub remote or not authenticated — skipping." >&2
  exit 0
fi

BRANCH=$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo main)

# Required contexts, from BOTH enforcement layers. setup-github.sh writes classic
# branch protection AND (org profile) a `project-init-baseline` repository ruleset,
# each carrying its own required_status_checks. Reading only the classic layer means
# a PR blocked solely by a stale RULESET check is told "all required checks are
# reported" — a false REASSURANCE, which is worse than the false alarm this script
# exists to avoid: it sends the operator looking anywhere but the real cause
# (PI-825).
#
# /rules/branches/<branch> is the authoritative view — the rules actually in force
# for that branch, whatever ruleset they came from. A repo with no protection (or a
# token without admin scope) errors on either call; that is not a failure, there is
# simply nothing to check.
CLASSIC=$(gh api "repos/$REPO/branches/$BRANCH/protection/required_status_checks" \
  --jq '.contexts[]?' 2>/dev/null || true)
# --paginate: /rules/branches is paginated, and a required_status_checks rule on a
# later page would be silently omitted — producing the same false "all healthy" this
# whole check exists to prevent.
RULESET=$(gh api --paginate "repos/$REPO/rules/branches/$BRANCH" \
  --jq '.[]? | select(.type == "required_status_checks")
        | .parameters.required_status_checks[]?.context' 2>/dev/null || true)
REQUIRED=$(printf '%s\n%s\n' "$CLASSIC" "$RULESET" | sed '/^$/d' | sort -u)
if [ -z "$REQUIRED" ]; then
  echo "check_branch_protection: no required status checks on $BRANCH — nothing to verify." >&2
  exit 0
fi

# Checks that actually reported, taken from the PR's rollup — the exact set GitHub
# matches the required contexts against.
#
# It MUST be a PR. A required context can legitimately be PR-only (validate-pr's
# "Check PR title, branch, and linked issue" never runs on a push to the default
# branch), so the branch's check-runs cannot tell a phantom from a PR-only check —
# they look identical: absent. An earlier version fell back to the branch and
# duly accused a perfectly satisfiable check of being unsatisfiable (PI-822).
#
# A false alarm here is worse than no diagnostic at all: it sends someone rewriting
# branch protection that was fine. So when there is no PR to look at, say so and
# stop, rather than guess.
if [ -z "$PR_NUMBER" ]; then
  PR_NUMBER=$(gh pr view --json number -q .number 2>/dev/null || true) # PR for this branch
fi
if [ -z "$PR_NUMBER" ]; then
  {
    echo "check_branch_protection: no pull request to check against."
    echo "  Pass a PR number: check-branch-protection.sh <PR>"
    echo "  A required check can be PR-only, so the default branch's check-runs"
    echo "  cannot distinguish a phantom from a check that simply never runs on a"
    echo "  push — and guessing would raise a false alarm."
  } >&2
  exit 0
fi

REPORTED=$(gh pr view "$PR_NUMBER" --json statusCheckRollup \
  --jq '.statusCheckRollup[]? | (.name // .context) | select(. != null)' 2>/dev/null || true)

PHANTOM=$(comm -23 <(printf '%s\n' "$REQUIRED" | sort -u) <(printf '%s\n' "$REPORTED" | sort -u))

if [ -z "$PHANTOM" ]; then
  echo "check_branch_protection: all required checks on $BRANCH are reported by CI."
  exit 0
fi

{
  echo ""
  echo "❌ branch protection requires checks that NOTHING reports:"
  printf '%s\n' "$PHANTOM" | sed 's/^/      - /'
  echo ""
  echo "   These can never be satisfied, so this PR — and every future PR — stays"
  echo "   BLOCKED, with every actual check green and no error anywhere."
  echo ""
  echo "   Usually the workflow's job names changed after branch protection was set"
  echo "   up (e.g. PI-761 made the Python matrix run per-PR on the floor version"
  echo "   only, so 'Lint and test (3.12)' and friends stopped being reported)."
  echo ""
  # --protect is REQUIRED: without it setup-github.sh never touches protection
  # or the ruleset, so the "remedy" would silently do nothing at all.
  echo "   Fix: re-run  .agents/scripts/setup-github.sh --protect"
  echo "   It requires the single 'CI gate' job, which needs: the whole matrix and"
  echo "   the secret scan — so it stays correct across any future matrix change."
} >&2
exit 1

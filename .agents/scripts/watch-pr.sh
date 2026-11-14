#!/usr/bin/env bash
# Wait for all CI checks on a PR, then optionally merge.
# Only prints failures or the final pass line — no per-refresh noise.
# Requires: gh, a Python 3 (resolved via ../hooks/python-resolve.sh; stdlib only — no jq).
#
# Usage:
#   .agents/scripts/watch-pr.sh <pr-number> [--merge] [--review-cycle N] [--no-review] [--admin]
#
# --merge: squash-merge and delete branch automatically when all checks pass.
# --review-cycle N: current review fix cycle count (0-based, default 0).
#   When N >= MAX_REVIEW_CYCLES and review/decision is still failing or pending,
#   force-merges with --admin.
# --no-review: skip all review waiting and admin-merge after CI passes.
#   Use ONLY for solo-dev PRs where no human reviewer will ever respond.
#   Do NOT use to avoid addressing legitimate review feedback.
# --admin: allow an admin override when the PR is BLOCKED by branch protection
#   AFTER the review gate passed (e.g. an unresolved conversation). Without it,
#   a post-review BLOCKED state fails with guidance instead of silently
#   overriding the protection setup-github.sh provisioned (2026-07 review).
#
# Full lifecycle for agents:
#   1. .agents/scripts/watch-pr.sh <n> --merge
#   2. Exit 2 -> review comments printed -> read and address them, push
#   3. Re-run with --review-cycle 1
#   4. Exit 2 again -> address remaining comments, push
#   5. Re-run with --review-cycle 2 -> admin-merge fires if still blocked
#
# Review cycle policy:
#   Two fix cycles are required before admin-merge is allowed. This ensures
#   review feedback (including Copilot comments) is read and addressed at
#   least once before force-merging.
#
# Ignored (informational) checks — PI-837:
#   `monitor_ignore_checks` in .agents/config.yaml (single-line JSON array of
#   check names; per-run override: PI_MONITOR_IGNORE_CHECKS, comma-separated)
#   names checks that are reported but never block the merge. Use it for a
#   known-dead check: during a GitHub Actions billing lockout, GitHub-hosted
#   jobs die permanently as zero-step startup failures while self-hosted CI
#   stays green — without this list one dead check deadlocks every PR.
#
#   Gotcha: check runs attach to COMMITS, not PRs. A sibling branch created at
#   the same head commit shares its check rollup — a failure produced by the
#   other branch's PR events shows up here too (observed: zarija #130/#131).
#   Remedy when it happens: give the PR a unique head, e.g.
#     git commit --allow-empty -m "chore: refresh PR head" && push
#   which clears the stale failure from this PR's rollup.

case "${1-}" in
-h | --help) # the header above is the help; nothing else runs (#992)
  sed -n '2,/^[^#]/s/^# \{0,1\}//p' "$0"
  exit 0
  ;;
esac
set -euo pipefail

# This script hard-requires the GitHub CLI (PI-362).
command -v gh >/dev/null 2>&1 || {
  echo "error: GitHub CLI (gh) not found — install: https://cli.github.com" >&2
  exit 1
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/gh-host.sh"

# Resolve the Python interpreter through the canonical helper (PI-361).
PY="$SCRIPT_DIR/../hooks/python-resolve.sh"

PR_NUMBER="${1:-}"
MODE=""
REVIEW_CYCLE=0
# #714: review-fix cycles before the script stops asking for another pass.
# Precedence: PI_REVIEW_CYCLES env > config.yaml `review_cycles` > 2.
# 0 disables review control entirely — merge as soon as CI is green.
MAX_REVIEW_CYCLES="${PI_REVIEW_CYCLES:-$(review_cycles)}"
case "$MAX_REVIEW_CYCLES" in
'' | *[!0-9]*)
  echo "review cycles must be a non-negative integer; got '$MAX_REVIEW_CYCLES'" >&2
  exit 2
  ;;
esac
# Base-10, so a config.yaml or PI_REVIEW_CYCLES carrying "08" can never reach an
# arithmetic context as octal (PR #717 review).
MAX_REVIEW_CYCLES=$((10#$MAX_REVIEW_CYCLES))
NO_REVIEW=0
ALLOW_ADMIN=0

if [ -z "$PR_NUMBER" ]; then
  echo "Usage: watch-pr.sh <pr-number> [--merge] [--review-cycle N] [--no-review] [--admin]" >&2
  exit 1
fi

# Parse flags (order-independent). --merge is just another flag: the usage
# line presents every flag as independent, so `watch-pr.sh 12 --no-review`
# must not be rejected for lacking --merge in position 2.
shift 1 # drop PR_NUMBER
while [ $# -gt 0 ]; do
  case "$1" in
  --merge)
    MODE="--merge"
    shift
    ;;
  --review-cycle)
    # Validate before `shift 2`: with no value, the shift fails under set -e
    # and aborts with no message (Copilot review).
    if [ $# -lt 2 ]; then
      echo "--review-cycle requires a numeric value" >&2
      exit 2
    fi
    REVIEW_CYCLE="$2"
    shift 2
    ;;
  --review-cycle=*)
    REVIEW_CYCLE="${1#*=}"
    shift
    ;;
  --no-review)
    NO_REVIEW=1
    shift
    ;;
  --admin)
    ALLOW_ADMIN=1
    shift
    ;;
  *)
    echo "Unknown option: $1" >&2
    exit 2
    ;;
  esac
done
case "$REVIEW_CYCLE" in
'' | *[!0-9]*)
  echo "--review-cycle must be a non-negative integer (got '$REVIEW_CYCLE')" >&2
  exit 2
  ;;
esac
# The digits-only check above accepts "08", which arithmetic expansion then reads
# as octal: `NEXT=$((REVIEW_CYCLE + 1))` dies with "value too great for base"
# (PR #717 review). `[ ]` comparisons parse base-10 and are unaffected, but the
# $(( )) sites are not — normalize once, here, rather than at each use.
REVIEW_CYCLE=$((10#$REVIEW_CYCLE))

# --admin, --no-review and --review-cycle only take effect while merging. Warn
# loudly if they were passed without --merge (e.g. `watch-pr.sh 12 --admin`)
# so the flag isn't silently a no-op — the script would otherwise just monitor
# and exit 0, looking like a successful merge that never happened.
if [ "$MODE" != "--merge" ]; then
  if [ "$ALLOW_ADMIN" -eq 1 ] || [ "$NO_REVIEW" -eq 1 ] || [ "$REVIEW_CYCLE" -ne 0 ]; then
    echo "WARNING: --admin/--no-review/--review-cycle only apply with --merge; ignoring (monitor-only run)." >&2
  fi
fi

# PI-837: checks treated as informational — reported, never blocking.
# Precedence: PI_MONITOR_IGNORE_CHECKS env (comma-separated) > config.yaml
# `monitor_ignore_checks` (single-line JSON array) > none. Normalized to a
# JSON array once, here; a malformed config value fails loudly rather than
# silently ignoring nothing.
IGNORE_CHECKS=$("$PY" -c "
import json, os, sys
env = os.environ.get('PI_MONITOR_IGNORE_CHECKS', '')
if env.strip():
    names = [n.strip() for n in env.split(',') if n.strip()]
else:
    raw = sys.argv[1].strip() or '[]'
    try:
        names = json.loads(raw)
        assert isinstance(names, list) and all(isinstance(n, str) for n in names)
    except Exception:
        print(f'monitor_ignore_checks in .agents/config.yaml must be a single-line'
              f' JSON array of check names; got: {raw}', file=sys.stderr)
        sys.exit(2)
print(json.dumps(names))
" "$(monitor_ignore_checks)")

_count_pending() {
  echo "$1" | "$PY" -c "
import json, sys
ignore = set(json.loads(sys.argv[1]))
data = json.load(sys.stdin)
# Exclude review/decision; it is a derived commit status that only appears
# after a review event. We detect review state directly via reviewDecision.
# Gate on gh's authoritative 'bucket' rollup (pass/fail/pending/skipping/
# cancel), not a hand-rolled state allowlist: just-queued Actions report
# state=QUEUED (and WAITING/REQUESTED for env-gated jobs), all bucket=pending.
# An allowlist that omitted those let the CI-wait break before CI ran (#428).
# Ignored checks are excluded too: a check the operator declared informational
# must not hang the CI wait any more than it may block the merge (PI-837).
print(sum(1 for c in data
          if c.get('name') != 'review/decision'
          and c.get('name') not in ignore
          and c.get('bucket') == 'pending'))
" "$IGNORE_CHECKS"
}

_print_failures() {
  echo "$1" | "$PY" -c "
import json, sys
ignore = set(json.loads(sys.argv[1]))
data = json.load(sys.stdin)
bad, informational = [], []
for c in data:
    if c.get('name') == 'review/decision':
        continue
    failing = (
        c.get('bucket') in ('fail', 'cancel')
        or c.get('state') in ('FAILURE', 'CANCELLED', 'TIMED_OUT', 'ERROR')
    )
    if not failing:
        continue
    (informational if c.get('name') in ignore else bad).append(c)
for c in informational:
    print(f\"  {c['name']}: {c.get('state') or c.get('bucket')} — informational (monitor_ignore_checks), not blocking\")
for c in bad:
    print(f\"  {c['name']}: {c.get('state') or c.get('bucket')}\")
sys.exit(len(bad))
" "$IGNORE_CHECKS"
}

# PI-939: read the derived review/decision commit status, or "" if absent.
_review_decision_state() {
  gh pr checks "$PR_NUMBER" --json name,bucket 2>/dev/null | "$PY" -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for c in data:
    if c.get('name') == 'review/decision':
        print(c.get('bucket') or '')
        break
" 2>/dev/null || true
}

# PI-939: re-trigger review-status.yml with a plain PR comment.
#
# review/decision is computed by a workflow, and the two events that SHOULD
# refresh it both fail to. Resolving a review thread emits no Actions event at
# all (`pull_request_review_thread` is a webhook event only, #719). And where
# the repository's Actions policy requires approval for bot actors, the
# `pull_request_review` run a bot reviewer triggers is queued at
# `action_required` — which cannot be cleared from the API either:
#
#   POST /actions/runs/{id}/approve
#   403  This run is not from a fork pull request or queued by the Actions bot
#
# So the status stays stale on a PR whose review gate has actually passed, and
# the merge below reports BLOCKED with no visible cause. `issue_comment` IS a
# trigger on that workflow and IS attributed to the commenter rather than a
# bot, so a comment settles it. That was a manual step; it is not any more.
_nudge_review_decision() {
  local state elapsed=0
  state=$(_review_decision_state)
  if [ "$state" = "pass" ]; then
    return 0
  fi
  echo "review/decision is '${state:-absent}' though the review gate passed — commenting to re-trigger review-status.yml (PI-939)."
  if ! gh pr comment "$PR_NUMBER" \
    --body "Review threads addressed and resolved. Re-triggering \`review/decision\`: resolving a thread emits no workflow event, so this comment is the documented nudge." \
    >/dev/null 2>&1; then
    echo "  could not post the comment — review/decision left as-is."
    return 0
  fi
  while [ "$elapsed" -lt 120 ]; do
    sleep 15
    elapsed=$((elapsed + 15))
    state=$(_review_decision_state)
    if [ "$state" = "pass" ]; then
      echo "  review/decision: pass after ${elapsed}s."
      return 0
    fi
  done
  # Not an error: the status may legitimately be failing, and the merge step
  # below reports that with its own diagnostics. Say what was tried.
  echo "  review/decision still '${state:-absent}' after ${elapsed}s — the merge may report BLOCKED."
}

# Print review feedback — inline comments first, falls back to full PR comments view.
_print_review_comments() {
  local inline
  inline=$(
    gh api "repos/{owner}/{repo}/pulls/$PR_NUMBER/comments" \
      --jq '.[] | "  \(.path):\(.line // "?") [\(.user.login)]\n  \(.body)\n"' \
      2>/dev/null || true
  )
  if [ -n "$inline" ]; then
    printf '%s\n' "$inline"
  else
    gh pr view "$PR_NUMBER" --comments 2>/dev/null || true
  fi
  echo "  Full PR: $(gh pr view "$PR_NUMBER" --json url -q '.url' 2>/dev/null || true)"
}

_run_gh() {
  local output
  local status

  set +e
  output=$(GH_PROMPT_DISABLED=1 gh "$@" 2>&1)
  status=$?
  set -e

  if [ -n "$output" ]; then
    printf '%s\n' "$output" | grep -v "^$" || true
  fi

  return "$status"
}

# PI-678: `--delete-branch` removes the remote branch, but the LOCAL branch
# survives whenever the merge was deferred (auto-merge), gh ran with another
# branch checked out, or the merge happened elsewhere — every merged PR left
# one behind for the operator to hand-delete. Clean it up when — and only
# when — the local branch is exactly what was merged: its SHA equals the PR's
# headRefOid (no unpushed work). Never touches any other branch or the base;
# skips silently when the branch is absent and loudly when it diverged.
_cleanup_local_branch() {
  local head_info head_ref head_oid base local_oid current
  # Only after the server confirms the merge: with a merge queue enabled,
  # a successful `gh pr merge` may have only ENQUEUED the still-open PR,
  # and the queue can still reject it (PR #707 review).
  _pr_is_merged || return 0
  head_info=$(GH_PROMPT_DISABLED=1 gh pr view "$PR_NUMBER" \
    --json headRefName,headRefOid -q '.headRefName + " " + .headRefOid' \
    2>/dev/null || true)
  head_ref=${head_info% *}
  head_oid=${head_info##* }
  { [ -n "$head_ref" ] && [ -n "$head_oid" ] && [ "$head_ref" != "$head_oid" ]; } || return 0
  if command -v base_branch >/dev/null 2>&1; then
    base=$(base_branch)
  else
    base="main"
  fi
  [ "$head_ref" = "$base" ] && return 0
  git show-ref --verify --quiet "refs/heads/$head_ref" || return 0
  local_oid=$(git rev-parse "refs/heads/$head_ref" 2>/dev/null || true)
  if [ "$local_oid" != "$head_oid" ]; then
    echo "  local branch $head_ref differs from the merged head — left in place."
    return 0
  fi
  current=$(git branch --show-current 2>/dev/null || true)
  if [ "$current" = "$head_ref" ]; then
    # Dirty worktree: skip silently (#678) — dirtiness only matters here,
    # where deleting would require switching branches under the user's feet.
    [ -z "$(git status --porcelain 2>/dev/null)" ] || return 0
    git checkout -q "$base" 2>/dev/null || return 0
    git pull -q --ff-only 2>/dev/null || true
  fi
  # Try the safe delete first; a squash merge leaves no ancestry so `-d`
  # refuses — then force, backed by the SHA equality above (nothing
  # unpushed) plus the server-confirmed merged state.
  if git branch -d "$head_ref" >/dev/null 2>&1 || git branch -D "$head_ref" >/dev/null 2>&1; then
    echo "  cleaned up local branch $head_ref"
  fi
}

# The PR being MERGED is success regardless of how the last attempt exited —
# "Merge already in progress" means the server accepted an earlier attempt.
_pr_is_merged() {
  # Best-effort probe: any failure (network, auth) reads as "not merged" —
  # the caller then keeps retrying or fails, never aborts under set -e.
  [ "$(GH_PROMPT_DISABLED=1 gh pr view "$PR_NUMBER" --json state -q .state 2>/dev/null || true)" = "MERGED" ]
}

# PI-632: the merge fires the instant the last check settles, but GitHub's
# mergeability computation lags a few seconds behind — the first attempt can
# fail ("Merge already in progress", "Pull Request is not mergeable") while
# every check is green and the PR is CLEAN. A manual re-run seconds later
# succeeded every time, so retry with backoff instead of declaring failure.
_merge_with_retry() {
  local delay delays
  delays="${PI_MERGE_RETRY_DELAYS:-5 10 20}"
  # word splitting of the delay list is intended
  # shellcheck disable=SC2086
  for delay in $delays; do
    if _run_gh pr merge "$PR_NUMBER" --squash --delete-branch; then
      return 0
    fi
    if _pr_is_merged; then
      echo "PR #$PR_NUMBER is already merged — treating as success."
      return 0
    fi
    echo "  merge not accepted yet — retrying in ${delay}s"
    sleep "$delay"
  done
  if _run_gh pr merge "$PR_NUMBER" --squash --delete-branch || _pr_is_merged; then
    return 0
  fi
  return 1
}

_admin_merge() {
  # Hard enforcement must bind under the org profile (ADR-013/#251): admin-merge
  # bypasses the server-side rules, so it is refused — use auto-merge / the merge
  # queue under the required checks instead. gh_profile comes from gh-host.sh.
  if [ "$(gh_profile)" = "org" ]; then
    echo "ERROR: admin-merge is refused under the org profile (#251) — hard" >&2
    echo "  enforcement must bind. Use auto-merge or the merge queue under the" >&2
    echo "  required checks, or resolve the blocking review/checks." >&2
    return 1
  fi
  if _run_gh pr merge "$PR_NUMBER" --squash --delete-branch --admin || _pr_is_merged; then
    echo "Merged PR #$PR_NUMBER (admin)"
    _cleanup_local_branch
  else
    echo "ERROR: admin merge failed for PR #$PR_NUMBER" >&2
    return 1
  fi
}

# Query the PR's aggregate review decision directly — source of truth regardless
# of whether the review/decision commit status has been posted yet.
# Returns: APPROVED | CHANGES_REQUESTED | REVIEW_REQUIRED | (empty = no review policy)
# Returns UNKNOWN on API failure — callers must treat this as fail-closed.
_get_review_decision() {
  gh pr view "$PR_NUMBER" --json reviewDecision -q '.reviewDecision // ""' 2>/dev/null || echo "UNKNOWN"
}

# Check whether a review OF THE HEAD COMMIT exists (COMMENTED, APPROVED, or
# CHANGES_REQUESTED). Bot reviewers like Codex post COMMENTED reviews that don't
# change reviewDecision, so we use this as an early exit signal from the wait loop.
#
# PI-981: this is the predicate review-status.yml posts as `review/decision`, so
# the monitor and the required check cannot disagree about whether THIS head was
# reviewed. A review counts only for the commit it names: a formal review by its
# REST `commit_id`, a comment-form Codex review by its "Reviewed commit:" line.
# The old `--json reviews` count had two faults. It counted every review ever
# posted, so after a push it called the new head reviewed while the required
# check was still waiting. And it could not see a comment-form review at all,
# so it waited out its timeout on PRs the gate had already passed (#982).
#
# PI-1003: and the review must be SOMEONE ELSE'S. GitHub records a reply to a
# review thread as a formal COMMENTED review by the replier, on the head the
# reply was written against, so the author's own reply — which the review
# protocol asks for on every comment — read as the review of a commit no
# reviewer had seen.
_has_review_activity() {
  local head nwo owner repo author formal codex
  head=$(gh pr view "$PR_NUMBER" --json headRefOid -q '.headRefOid' 2>/dev/null) || return 1
  # The SHA is spliced into the jq programs below, so it must be exactly one.
  case "$head" in
  '' | *[!0-9a-f]*) return 1 ;;
  esac
  [ "${#head}" -eq 40 ] || return 1
  nwo=$(gh repo view --json nameWithOwner -q '.nameWithOwner' 2>/dev/null) || return 1
  owner=${nwo%%/*}
  repo=${nwo##*/}
  # The author is read from REST, because it is compared against logins REST
  # also spells: `gh pr view --json author` would give a bot's login without the
  # `[bot]` suffix REST appends, and a bot-authored PR would then compare unequal
  # for ever. Each leg lists the logins that reviewed the head and drops the
  # author's own lines with a fixed whole-line match, so no login is ever spliced
  # into a jq program — jq sees only the head SHA, validated above. The comment
  # leg compares against the login with `[bot]` trimmed, because THAT one comes
  # back from GraphQL, which spells it bare.
  #
  # An author that cannot be read counts NO review at all, on either leg: with no
  # author known, "by somebody other than the author" is unanswerable for every
  # review, and review-status.yml answers it the same way — so the monitor and
  # the required check cannot disagree.
  author=$(gh api "repos/$owner/$repo/pulls/$PR_NUMBER" --jq '.user.login' 2>/dev/null) || author=""
  formal=0
  codex=0
  if [ -n "$author" ]; then
    # Active submitted states only: a DISMISSED review was revoked (#1036).
    formal=$(gh api --paginate "repos/$owner/$repo/pulls/$PR_NUMBER/reviews" \
      --jq ".[] | select(.commit_id == \"$head\" and (.state | IN(\"APPROVED\", \"CHANGES_REQUESTED\", \"COMMENTED\"))) | .user.login" \
      2>/dev/null | grep -cvxF -- "$author") || formal=0
    codex=$(gh api graphql --paginate \
      -F owner="$owner" -F repo="$repo" -F number="$PR_NUMBER" -f query='
        query($owner:String!, $repo:String!, $number:Int!, $endCursor:String) {
          repository(owner:$owner, name:$repo) {
            pullRequest(number:$number) {
              comments(first:100, after:$endCursor) {
                pageInfo { hasNextPage endCursor }
                nodes { author { login } body }
              }
            }
          }
        }' \
      --jq ".data.repository.pullRequest.comments.nodes[]
            | select(.author.login == \"chatgpt-codex-connector\")
            | select(.body | sub(\"^[[:space:]]+\"; \"\") | ascii_downcase | startswith(\"codex review:\"))
            | select([.body | match(\"Reviewed commit:[*]*[[:space:]]*\`([0-9a-fA-F]{7,40})\`\").captures[0].string
                      | ascii_downcase] | any(. as \$c | \"$head\" | startswith(\$c)))
            | .author.login" \
      2>/dev/null | grep -cvxF -- "${author%\[bot\]}") || codex=0
  fi
  [ "$((formal + codex))" -gt 0 ]
}

# PI-715: count review threads nobody has resolved. On solo profiles there is no
# approving review to gate on (see the empty-reviewDecision branch below), so
# "every review comment is resolved" IS the review gate. GitHub enforces the same
# thing server-side via required_conversation_resolution; checking it here turns
# a late, opaque "merge blocked" into an actionable review cycle.
# Echoes a count, or nothing when the query fails — callers treat that as unknown.
_unresolved_threads() {
  local nwo owner repo
  nwo=$(gh repo view --json nameWithOwner -q '.nameWithOwner' 2>/dev/null) || return 0
  owner=${nwo%%/*}
  repo=${nwo##*/}
  gh api graphql -F owner="$owner" -F repo="$repo" -F number="$PR_NUMBER" -f query='
    query($owner:String!, $repo:String!, $number:Int!) {
      repository(owner:$owner, name:$repo) {
        pullRequest(number:$number) {
          reviewThreads(first:100) { nodes { isResolved } }
        }
      }
    }' --jq '[.data.repository.pullRequest.reviewThreads.nodes[]
             | select(.isResolved == false)] | length' 2>/dev/null || true
}

# PI-706: `gh pr checks` reports the rollup for whatever commit the API believes
# is the PR head. Right after a push the API can still serve the PREVIOUS
# headRefOid (replication lag); that commit's checks are already settled, so the
# CI wait below breaks on the first poll and judges the wrong commit — a red
# predecessor reads as "CI failed" (observed on #705), and a green one would
# merge a commit whose CI never ran. `git ls-remote` answers from git's endpoint
# rather than the API, so it sees the pushed tip immediately: wait until the two
# agree before any check result is trusted.
#
# Two distinct outcomes when the SHAs don't line up:
#   * skipped entirely (return 0, unchanged behavior) when no expected SHA can
#     be established — a cross-repo PR, a deleted branch, no `origin`, or the
#     gate switched off with PI_HEAD_SYNC_TIMEOUT=0;
#   * fail closed (exit 1) when an expected SHA IS known and the API never
#     catches up to it within the timeout, since every check result on hand
#     may belong to some other commit.
_wait_for_head_sync() {
  local head_info head_ref cross_repo remote_sha api_sha elapsed
  if [ "$HEAD_SYNC_TIMEOUT" -eq 0 ]; then
    return 0
  fi
  # Cross-repo (fork) PRs: headRefName carries only the branch name, so
  # `ls-remote origin refs/heads/$head_ref` would resolve a base-repo branch
  # that merely shares the name (`main`, `feature`) and then wait out the
  # timeout against a SHA from the wrong repository (PR #712 review). The
  # fork's remote isn't configured here — skip rather than guess.
  head_info=$(GH_PROMPT_DISABLED=1 gh pr view "$PR_NUMBER" \
    --json headRefName,isCrossRepository \
    -q '.headRefName + " " + (.isCrossRepository | tostring)' 2>/dev/null || true)
  [ -n "$head_info" ] || return 0
  head_ref=${head_info% *}
  cross_repo=${head_info##* }
  if [ "$cross_repo" = "true" ]; then
    return 0
  fi
  [ -n "$head_ref" ] || return 0
  remote_sha=$(git ls-remote origin "refs/heads/$head_ref" 2>/dev/null | cut -f1 || true)
  [ -n "$remote_sha" ] || return 0
  elapsed=0
  while true; do
    api_sha=$(GH_PROMPT_DISABLED=1 gh pr view "$PR_NUMBER" --json headRefOid \
      -q '.headRefOid' 2>/dev/null || true)
    if [ "$api_sha" = "$remote_sha" ]; then
      return 0
    fi
    if [ "$elapsed" -ge "$HEAD_SYNC_TIMEOUT" ]; then
      echo "PR #$PR_NUMBER: GitHub still reports head ${api_sha:-<unknown>}, but the" >&2
      echo "  remote branch $head_ref is at $remote_sha (${HEAD_SYNC_TIMEOUT}s elapsed)." >&2
      echo "  Refusing to judge check results that may belong to another commit." >&2
      echo "  Re-run shortly, or set PI_HEAD_SYNC_TIMEOUT=0 to skip this gate." >&2
      exit 1
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
}

# --- Wait for all CI checks (excludes review/decision commit status) ---
# Guard: if checks haven't registered yet (empty list), keep polling.
# An empty list is indistinguishable from "all done" without this guard,
# which caused premature merges before CI even started.
# Bounded wait (PI-186): a required check that never leaves PENDING/EXPECTED
# (e.g. a workflow that never triggers on this branch) must not hang the
# script — and any autonomous caller — forever. Fail closed on timeout.
# PI-674: overridable — a single self-hosted runner serializes jobs past
# 900s; set PI_CI_TIMEOUT (seconds) in the environment, or register a second
# runner to restore parallelism.
CI_TIMEOUT="${PI_CI_TIMEOUT:-900}"
case "$CI_TIMEOUT" in
'' | *[!0-9]*)
  echo "PI_CI_TIMEOUT must be a positive integer (seconds); got '${PI_CI_TIMEOUT:-}'" >&2
  exit 2
  ;;
esac
if [ "$CI_TIMEOUT" -eq 0 ]; then
  echo "PI_CI_TIMEOUT must be a positive integer (seconds); got '${PI_CI_TIMEOUT:-}'" >&2
  exit 2
fi

# PI-706: how long to wait for the API's PR head to catch up with the pushed
# tip. 0 disables the gate — an escape hatch for mirrors and other setups where
# `git ls-remote origin` reports a SHA the API will never converge on.
HEAD_SYNC_TIMEOUT="${PI_HEAD_SYNC_TIMEOUT:-120}"
case "$HEAD_SYNC_TIMEOUT" in
'' | *[!0-9]*)
  echo "PI_HEAD_SYNC_TIMEOUT must be a non-negative integer (seconds); got '${PI_HEAD_SYNC_TIMEOUT:-}'" >&2
  exit 2
  ;;
esac
_wait_for_head_sync

CI_ELAPSED=0
while true; do
  CHECKS=$(gh pr checks "$PR_NUMBER" --json name,state,bucket 2>/dev/null) || CHECKS="[]"
  # PI-858: the "checks registered" guard must count only gate-relevant checks
  # — the same filter _count_pending applies (no review/decision, no ignored
  # names). An ignored check registers FIRST in practice (board-sync fires on
  # PR-open while real CI jobs are still queueing), and a raw length here let
  # the wait break on that ignored-only rollup: _print_failures then saw no
  # blocking failure and --merge proceeded with real CI never having run.
  # If only-ignored checks ever exist, the timeout below fails closed.
  CHECK_COUNT=$(echo "$CHECKS" | "$PY" -c "
import json, sys
ignore = set(json.loads(sys.argv[1]))
data = json.load(sys.stdin)
print(sum(1 for c in data
          if c.get('name') != 'review/decision'
          and c.get('name') not in ignore))
" "$IGNORE_CHECKS")
  if [ "$CHECK_COUNT" -gt 0 ] && [ "$(_count_pending "$CHECKS")" -eq 0 ]; then
    break
  fi
  if [ "$CI_ELAPSED" -ge "$CI_TIMEOUT" ]; then
    echo "PR #$PR_NUMBER: CI did not settle within ${CI_TIMEOUT}s. Still pending:"
    echo "$CHECKS" | "$PY" -c "import json,sys
for c in json.load(sys.stdin):
    if c.get('name') != 'review/decision' and c.get('bucket') == 'pending':
        print('  -', c.get('name'))" 2>/dev/null || true
    echo "Re-run once the check registers, or investigate why it never started."
    # PI-671: a billing/minutes lockout means checks NEVER register — point
    # at the escape hatch instead of leaving the user to rediscover it.
    if gh run list --limit 5 --json conclusion --jq '.[].conclusion' 2>/dev/null | grep -q startup_failure; then
      echo "Note: recent runs show startup_failure — GitHub Actions may be out of"
      echo "minutes or billing-locked. Load the local-ci skill (self-hosted runner"
      echo "escape hatch: just ci-local-on)."
    fi
    exit 1
  fi
  sleep 10
  CI_ELAPSED=$((CI_ELAPSED + 10))
done

FAIL_CODE=0
_print_failures "$CHECKS" || FAIL_CODE=$?

if [ "$FAIL_CODE" -gt 0 ]; then
  echo "CI failed on PR #$PR_NUMBER — fix the issues, push, then re-run this script."
  exit 1
fi

# --no-review: explicit bypass — skip review gate entirely after CI passes.
# Use only for solo-dev PRs where no reviewer will ever respond.
#
# It skips WAITING for a reviewer. It does not bypass branch protection, and until
# 2026-08-11 it did: this branch went straight to _admin_merge, so on any host whose
# profile refuses admin-merge the flag could never merge anything. A green, unblocked PR
# needs the same plain squash the --merge path performs at the bottom of this script, and
# routing it through the override made the one flag documented for solo repos the one
# flag guaranteed to fail there.
if [ "$NO_REVIEW" -eq 1 ] && [ "$MODE" = "--merge" ]; then
  echo "PR #$PR_NUMBER: CI passed. --no-review specified — skipping review gate."
  MERGE_STATE=$(gh pr view "$PR_NUMBER" --json mergeStateStatus -q '.mergeStateStatus' 2>/dev/null || echo "UNKNOWN")
  if [ "$MERGE_STATE" = "CLEAN" ] || [ "$MERGE_STATE" = "UNSTABLE" ]; then
    if _merge_with_retry; then
      echo "Merged PR #$PR_NUMBER"
      _cleanup_local_branch
      exit 0
    fi
    echo "ERROR: merge failed for PR #$PR_NUMBER" >&2
    exit 1
  fi
  # Anything else is a server-side rule the review gate cannot satisfy, which is what
  # --admin is for — and what _admin_merge refuses where hard enforcement must bind.
  echo "PR #$PR_NUMBER is $MERGE_STATE — a plain merge will not take it."
  _admin_merge
  exit 0
fi

# --- Wait up to 6 min for a reviewer to act ---
# Query reviewDecision directly so this works before review-status.yml creates
# the derived review/decision commit status.
REVIEW_TIMEOUT=360
REVIEW_ELAPSED=0
REVIEW_DECISION=$(_get_review_decision)

# #714: review_cycles=0 means the operator declined review control. Skip the whole
# review gate — the wait, the changes-requested cycle, the unresolved-thread check
# — and merge on green CI. Deliberately NOT an admin override: if an approval
# policy is in force the merge still blocks, because "no review control" is a
# choice about this script's cycles, not a licence to bypass branch protection.
# Set before the max-cycles check below, which would otherwise read 0 >= 0 as
# "cycles exhausted" and force an admin merge.
# Not gated on --merge: a monitor-only run would otherwise sit in the reviewer
# wait loop for a gate the operator switched off (PR #717 review).
if [ "$MAX_REVIEW_CYCLES" -eq 0 ]; then
  if [ "$MODE" = "--merge" ]; then
    echo "PR #$PR_NUMBER: CI passed. review_cycles=0 — no review control; merging on green CI."
  else
    echo "PR #$PR_NUMBER: CI passed. review_cycles=0 — no review control; skipping the review gate."
  fi
  REVIEW_DECISION="SKIPPED"
fi

if [ "$MODE" = "--merge" ] && [ "$REVIEW_CYCLE" -ge "$MAX_REVIEW_CYCLES" ] && [ "$REVIEW_DECISION" = "REVIEW_REQUIRED" ]; then
  echo "Max review cycles ($MAX_REVIEW_CYCLES) reached — skipping reviewer wait and force-merging with admin override."
  _admin_merge
  exit 0
fi

if [ "$REVIEW_DECISION" = "REVIEW_REQUIRED" ] || [ "$REVIEW_DECISION" = "UNKNOWN" ]; then
  echo "Waiting for reviewer (up to ${REVIEW_TIMEOUT}s, polling every 30s) — reviewDecision: ${REVIEW_DECISION}"
fi

# PI-715: an EMPTY reviewDecision means the branch has no approval policy — the
# solo-profile default, since an approving review is unsatisfiable there (GitHub
# refuses self-approval; Copilot/Codex only ever COMMENT). "No approval needed"
# must not collapse into "merge with zero review": wait for a review to land,
# on the same budget as the approval wait above.
if [ -z "$REVIEW_DECISION" ] && [ "$MODE" = "--merge" ]; then
  if ! _has_review_activity; then
    echo "Waiting for a review (up to ${REVIEW_TIMEOUT}s, polling every 30s) — no approval policy on this branch"
    while ! _has_review_activity && [ "$REVIEW_ELAPSED" -lt "$REVIEW_TIMEOUT" ]; do
      sleep 30
      REVIEW_ELAPSED=$((REVIEW_ELAPSED + 30))
    done
  fi
  # PR #716 review (P1): the decision was read before any review existed. A
  # review that lands during — or just before — the wait may be
  # CHANGES_REQUESTED, and a summary-only change request leaves no unresolved
  # thread behind. Without this re-read, REVIEW_DECISION stays empty, the
  # CHANGES_REQUESTED block below is skipped, and the PR merges over a
  # requested change. Re-read so that block sees it.
  REVIEW_DECISION=$(_get_review_decision)
fi
# Token-efficiency (PI-653, epic #641): the poll loop echoes a frame only when
# reviewDecision CHANGES — identical repeated frames persist in the agent's
# transcript and are re-sent every turn. Terminal summaries are unchanged.
LAST_DECISION="$REVIEW_DECISION"
while { [ "$REVIEW_DECISION" = "REVIEW_REQUIRED" ] || [ "$REVIEW_DECISION" = "UNKNOWN" ]; } && [ "$REVIEW_ELAPSED" -lt "$REVIEW_TIMEOUT" ]; do
  sleep 30
  REVIEW_ELAPSED=$((REVIEW_ELAPSED + 30))
  REVIEW_DECISION=$(_get_review_decision)
  if [ "$REVIEW_DECISION" != "$LAST_DECISION" ]; then
    echo "  [${REVIEW_ELAPSED}s/${REVIEW_TIMEOUT}s] reviewDecision: ${REVIEW_DECISION:-none}"
    LAST_DECISION="$REVIEW_DECISION"
  fi
  # Early exit: if any review activity exists (even COMMENTED), stop waiting.
  # Bot reviewers like Codex post comments without changing reviewDecision.
  if [ "$REVIEW_DECISION" = "REVIEW_REQUIRED" ] && _has_review_activity; then
    echo "  Review comments detected — proceeding without waiting for formal approval."
    REVIEW_ACTIVITY=1
    break
  fi
  CHECKS=$(gh pr checks "$PR_NUMBER" --json name,state,bucket 2>/dev/null) || CHECKS="[]"
done

if [ "$REVIEW_DECISION" = "UNKNOWN" ]; then
  echo "ERROR: could not fetch reviewDecision for PR #$PR_NUMBER — cannot verify review state." >&2
  exit 2
fi

if [ "$REVIEW_DECISION" = "CHANGES_REQUESTED" ]; then
  echo "Review/decision failed on PR #$PR_NUMBER (cycle $REVIEW_CYCLE/$MAX_REVIEW_CYCLES):"
  _print_review_comments

  if [ "$MODE" = "--merge" ]; then
    if [ "$REVIEW_CYCLE" -ge "$MAX_REVIEW_CYCLES" ]; then
      echo "Max review cycles ($MAX_REVIEW_CYCLES) reached — force-merging with admin override."
      _admin_merge
      exit 0
    else
      NEXT=$((REVIEW_CYCLE + 1))
      echo "Address the comments above, push your changes, then re-run:"
      echo "  .agents/scripts/watch-pr.sh $PR_NUMBER --merge --review-cycle $NEXT"
      exit 2
    fi
  fi
  exit 1
fi

if [ "$REVIEW_DECISION" = "REVIEW_REQUIRED" ]; then
  if [ "${REVIEW_ACTIVITY:-0}" -eq 1 ]; then
    # The wait loop broke early because reviews were posted (e.g. a bot's
    # COMMENTED review) — surface them instead of claiming nobody acted.
    echo "PR #$PR_NUMBER: review comments posted, but no formal approval yet:"
    _print_review_comments
  else
    echo "PR #$PR_NUMBER: review/decision still pending after ${REVIEW_TIMEOUT}s — no reviewer has acted."
    echo "  Full PR: $(gh pr view "$PR_NUMBER" --json url -q '.url' 2>/dev/null || true)"
  fi

  if [ "$MODE" = "--merge" ]; then
    if [ "$REVIEW_CYCLE" -ge "$MAX_REVIEW_CYCLES" ]; then
      echo "Max review cycles ($MAX_REVIEW_CYCLES) reached — force-merging with admin override."
      _admin_merge
      exit 0
    else
      NEXT=$((REVIEW_CYCLE + 1))
      echo "Request a review or wait for a reviewer, then re-run:"
      echo "  .agents/scripts/watch-pr.sh $PR_NUMBER --merge --review-cycle $NEXT"
      exit 2
    fi
  fi
  exit 1
fi

# PI-715: the no-approval-policy gate. reviewDecision is empty, so nothing above
# blocked — enforce the agent-review protocol here instead: a review must have
# landed, and no review thread may be left unresolved. This is the same contract
# required_conversation_resolution enforces server-side, surfaced as a review
# cycle rather than an opaque "merge blocked" at the last step.
if [ -z "$REVIEW_DECISION" ] && [ "$MODE" = "--merge" ]; then
  UNRESOLVED=$(_unresolved_threads)
  if [ "${UNRESOLVED:-0}" -gt 0 ]; then
    echo "PR #$PR_NUMBER: ${UNRESOLVED} unresolved review comment(s) (cycle $REVIEW_CYCLE/$MAX_REVIEW_CYCLES):"
    _print_review_comments
    NEXT=$((REVIEW_CYCLE + 1))
    echo "Address them, resolve the threads, push, then re-run:"
    echo "  .agents/scripts/watch-pr.sh $PR_NUMBER --merge --review-cycle $NEXT"
    # Never force past unresolved comments: required_conversation_resolution
    # would reject the merge anyway, and --admin here would silently discard
    # feedback nobody answered.
    exit 2
  fi
  # PI-838: the reviewer-absent path must terminate, and say what it is. With
  # no approval policy the max-cycles→admin-merge branch above (REVIEW_REQUIRED
  # only) never applies; this branch is the terminal state for a review agent
  # that never responds — observed live as a quota-limited bot ignoring an
  # explicit re-request for hours. Name the condition (CI is green; this is
  # not a CI problem), say what cycle exhaustion does, and name --no-review —
  # don't imply a convergence that structurally cannot happen.
  if ! _has_review_activity; then
    if [ "$REVIEW_CYCLE" -lt "$MAX_REVIEW_CYCLES" ]; then
      NEXT=$((REVIEW_CYCLE + 1))
      echo "PR #$PR_NUMBER: no review of the head commit has landed after ${REVIEW_TIMEOUT}s (cycle $REVIEW_CYCLE/$MAX_REVIEW_CYCLES)."
      echo "CI is green — the review agent has not acted on this head. A review of an earlier commit does not count (PI-981), and a reply you posted on a review thread is not a review of it (PI-1003); a PR comment reading '@codex review' asks for one."
      echo "A quota-limited bot (e.g. a Codex daily cap) may resume later. Re-run to give it another window:"
      echo "  .agents/scripts/watch-pr.sh $PR_NUMBER --merge --review-cycle $NEXT"
      echo "After cycle $MAX_REVIEW_CYCLES with no review and no approval policy, the merge proceeds with a REVIEWER ABSENT warning."
      echo "--no-review skips the review gate entirely."
      exit 2
    fi
    echo "WARNING: REVIEWER ABSENT — no review of the head commit landed within $MAX_REVIEW_CYCLES cycles and this branch has no approval policy."
    echo "  Merging on green CI. Consider a follow-up review once the review agent recovers."
  fi
fi

PR_URL=$(gh pr view "$PR_NUMBER" --json url -q '.url')
echo "PR #$PR_NUMBER passed: $PR_URL"

# The review gate above has passed. If the derived status has not caught up,
# nudge it before attempting the merge — not after, when the failure mode is an
# opaque BLOCKED. Skipped when the operator turned the review gate off, because
# there is then no gate whose result the status is lagging behind.
if [ "$MODE" = "--merge" ] && [ "$MAX_REVIEW_CYCLES" -ne 0 ] && [ "$REVIEW_DECISION" != "SKIPPED" ]; then
  _nudge_review_decision
fi

if [ "$MODE" = "--merge" ]; then
  MERGE_STATE=$(gh pr view "$PR_NUMBER" --json mergeStateStatus -q '.mergeStateStatus' 2>/dev/null || echo "UNKNOWN")

  if [ "$MERGE_STATE" = "CLEAN" ] || [ "$MERGE_STATE" = "UNSTABLE" ]; then
    if _merge_with_retry; then
      echo "Merged PR #$PR_NUMBER"
      _cleanup_local_branch
    else
      echo "ERROR: merge failed for PR #$PR_NUMBER" >&2
      exit 1
    fi
  elif [ "$MERGE_STATE" = "BLOCKED" ]; then
    # The review gate already passed to reach here, so BLOCKED means a DIFFERENT
    # branch-protection rule is unmet — most often an unresolved review thread
    # (required_conversation_resolution, which setup-github.sh --protect itself
    # provisions). Auto-admin-merging past it on cycle 0 silently defeats the
    # protection this very tool set up, so it now requires an explicit --admin
    # opt-in (2026-07 review).
    if [ "$ALLOW_ADMIN" -eq 1 ]; then
      echo "PR is blocked by branch protection — merging with admin override (--admin)."
      _admin_merge
    else
      echo "ERROR: PR #$PR_NUMBER is BLOCKED by branch protection after the review gate" >&2
      echo "  passed — typically an unresolved review conversation, or a required" >&2
      echo "  check that has not reported. Resolve the blocker, or re-run with --admin" >&2
      echo "  to override (refused under the org profile, #251)." >&2
      # "A required check that has not reported" is the one blocker with no visible
      # cause: CI is green, nothing errors, and the PR is simply never mergeable
      # (PI-819). Name the phantom contexts rather than leaving it to be guessed.
      _SELF_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
      if [ -x "$_SELF_DIR/check-branch-protection.sh" ]; then
        "$_SELF_DIR/check-branch-protection.sh" "$PR_NUMBER" || true
      fi
      exit 1
    fi
  else
    if ! _merge_with_retry; then
      if ! _run_gh pr merge "$PR_NUMBER" --squash --delete-branch --auto; then
        echo "ERROR: could not merge or enable auto-merge for PR #$PR_NUMBER" >&2
        exit 1
      fi
      echo "Auto-merge enabled for PR #$PR_NUMBER — will merge once all requirements are met."
    else
      echo "Merged PR #$PR_NUMBER"
      _cleanup_local_branch
    fi
  fi
fi

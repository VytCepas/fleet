#!/usr/bin/env bash
# Start work on a GitHub issue: create branch, push, and open a draft PR.
#
# Usage:
#   .agents/scripts/start-issue.sh <issue-number> <type>
#
# Types: feat  fix  chore  docs  test
#
# Composable with create-issue.sh:
#   .agents/scripts/create-issue.sh feat "Add OAuth login" | xargs -I{} .agents/scripts/start-issue.sh {} feat

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

# Anchor sibling-script and config lookups on this script's location, not the
# caller's cwd — invoked from a subdirectory, a cwd-relative path would derive
# the wrong project key and then die at the push-branch.sh call, stranding the
# repo on a fresh unpushed branch with no PR.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Resolve the base branch (ADR-014) from the promotion chain via gh-host.sh.
source "$SCRIPT_DIR/gh-host.sh"

VALID_TYPES="feat fix chore docs test"

usage() {
  echo "Usage: start-issue.sh <issue-number> <type>"
  echo ""
  echo "Types: $VALID_TYPES"
  echo ""
  echo "Examples:"
  echo "  start-issue.sh 42 feat"
  echo "  start-issue.sh 99 fix"
  exit 1
}

# --- Validate args ---
if [ $# -lt 2 ]; then
  usage
fi

ISSUE_NUMBER="$1"
TYPE="$2"

if ! echo "$VALID_TYPES" | grep -qw "$TYPE"; then
  echo "ERROR: invalid type '$TYPE'. Valid types: $VALID_TYPES" >&2
  exit 1
fi

if ! [[ "$ISSUE_NUMBER" =~ ^[0-9]+$ ]]; then
  echo "ERROR: issue number must be numeric, got '$ISSUE_NUMBER'" >&2
  exit 1
fi

# --- Resolve project key / abbreviation ---
# Set PROJECT_KEY env var, add `project_key: PI` to .agents/config.yaml,
# or let the script derive one from the repository directory name.

# Name of the repository's MAIN worktree directory — never the current one.
# Inside a linked worktree (`git worktree add ../zari-15-synthetic`),
# `--show-toplevel` returns the worktree directory, so the derived key changes
# per worktree (Z1S vs ZARI) and one repo accumulates mixed branch/PR keys
# (#631). The common git dir always lives under the main worktree, so anchor
# the name there.
_repo_root_name() {
  local common="" base=""
  common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
  if [ -z "$common" ]; then
    # git < 2.31 lacks --path-format; the raw value may be relative to cwd.
    common=$(git rev-parse --git-common-dir 2>/dev/null || true)
    case "$common" in
    "" | /*) ;;
    *) common="$(pwd)/$common" ;;
    esac
  fi
  if [ -n "$common" ]; then
    base=$(basename "$common")
    if [ "$base" = ".git" ] && [ -d "$(dirname "$common")" ]; then
      # Normal layout: the common dir is <main-worktree>/.git — the repo
      # name is its parent directory's.
      basename "$(cd "$(dirname "$common")" && pwd)"
      return
    fi
    case "$base" in
    ?*.git)
      # Bare repo (worktrees hang off /srv/repos/widget.git): the repo name
      # is the git dir itself minus the .git suffix — its parent directory
      # is unrelated (PR #702 review).
      printf '%s\n' "${base%.git}"
      return
      ;;
    esac
  fi
  # Separate/custom git dir (GIT_DIR=...): no repo name to infer from it —
  # fall back to the current worktree's own name.
  basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
}

derive_project_key() {
  if [ -n "${PROJECT_KEY:-}" ]; then
    echo "$PROJECT_KEY"
    return
  fi

  local configured=""
  configured=$(grep '^[[:space:]]*project_key:' "$SCRIPT_DIR/../config.yaml" 2>/dev/null |
    head -n 1 |
    cut -d: -f2- |
    sed 's/#.*$//' |
    tr -d '[:space:]"' |
    tr -d "'" || true)
  if [ -n "$configured" ]; then
    echo "$configured"
    return
  fi

  local repo_name=""
  repo_name=$(_repo_root_name)
  echo "$repo_name" |
    tr '[:lower:]' '[:upper:]' |
    tr -cs 'A-Z0-9' '\n' |
    awk 'NF { printf substr($0, 1, 1) }' |
    cut -c1-10
}

PROJECT_KEY=$(derive_project_key)
PROJECT_KEY=$(echo "$PROJECT_KEY" | tr '[:lower:]' '[:upper:]' | tr -cd 'A-Z0-9')
# A single-word repo name yields a 1-char initials key (e.g. "widget" -> "W").
# That passes the branch-name regex but the commit-msg hook then rejects every
# commit — its scope regex requires >=2 chars: [A-Z][A-Z0-9]{1,9}- (#432).
# Widen a too-short key to the repo name's leading alphanumerics first.
if [ "${#PROJECT_KEY}" -lt 2 ]; then
  PROJECT_KEY=$(_repo_root_name |
    tr '[:lower:]' '[:upper:]' | tr -cd 'A-Z0-9' | cut -c1-4)
fi
# Final guard: the key must satisfy the shared key regex (>=2 chars, leading
# letter). Empty, still-too-short, or digit-leading -> stable PROJ fallback.
if ! echo "$PROJECT_KEY" | grep -qE '^[A-Z][A-Z0-9]{1,9}$'; then
  PROJECT_KEY="PROJ"
fi

# --- Fetch issue title ---
# `|| true`: under set -e a failing gh would kill the script at this assignment
# before the error branch below could run. Capture stderr to a file (NOT 2>&1 —
# that would mix gh's error text into ISSUE_TITLE and defeat the empty-check)
# so an auth/network failure is reported as itself, not as "issue not found"
# (Copilot review).
GH_ERR=$(mktemp)
ISSUE_TITLE=$(gh issue view "$ISSUE_NUMBER" --json title -q '.title' 2>"$GH_ERR" || true)
if [ -z "$ISSUE_TITLE" ]; then
  echo "ERROR: could not fetch issue #$ISSUE_NUMBER — it may not exist, or gh failed:" >&2
  sed 's/^/  /' "$GH_ERR" >&2
  rm -f "$GH_ERR"
  exit 1
fi
rm -f "$GH_ERR"

ISSUE_REF="${PROJECT_KEY}-${ISSUE_NUMBER}"

# --- Derive branch name: <issue_type>/<project_abbr>-<issue_number>-<kebab-slug>, max 80 chars total ---
# Matches convention: feat/PI-42-add-oauth-login, fix/API-99-null-pointer
# Strip leading [type] prefix from issue title if present (e.g. "[feat] Add OAuth" -> "Add OAuth")
CLEAN_TITLE=$(echo "$ISSUE_TITLE" | sed 's/^\[[^]]*\] *//')
SLUG=$(echo "$CLEAN_TITLE" |
  tr '[:upper:]' '[:lower:]' |
  tr -cs 'a-z0-9' '-' |
  sed 's/^-//;s/-$//')
PREFIX="${ISSUE_REF}-"
MAX_SLUG=$((80 - ${#TYPE} - 1 - ${#PREFIX})) # -1 for the /
if [ "$MAX_SLUG" -lt 12 ]; then
  MAX_SLUG=12
fi
SLUG="${SLUG:0:$MAX_SLUG}"
SLUG="${SLUG%-}" # trim trailing dash if truncated mid-word
# A title with no alphanumerics (e.g. "!!!") collapses to an empty slug, so the
# branch would be `feat/PI-42-` — pushed and PR'd before validate-pr.yml rejects
# it for a slug that doesn't start with [a-z0-9]. Fall back to a stable slug so
# no malformed branch is created (PI-206).
[ -z "$SLUG" ] && SLUG="issue"
BRANCH="${TYPE}/${PREFIX}${SLUG}"

echo "Branch: $BRANCH"

# --- Guard: already on this branch or it already exists ---
CURRENT=$(git branch --show-current)
if [ "$CURRENT" = "$BRANCH" ]; then
  echo "Already on branch $BRANCH"
elif git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  echo "Branch $BRANCH already exists - switching"
  git checkout "$BRANCH"
else
  git checkout -b "$BRANCH"
fi

# --- Seed an empty commit so the draft PR has a diff base ---
# GitHub refuses a PR with no commits between base and head ("No commits between
# main and <branch>"), so a freshly-created branch cannot open the draft PR this
# script promises until it has >=1 commit. Seed one when the branch is level
# with the base; real work simply adds commits on top (#433).
#
# Build the seed from HEAD's own tree with commit-tree so it is empty by
# construction and cannot capture the user's staged index — a plain
# `git commit --allow-empty` still commits whatever is currently staged, which
# would silently fold unrelated work into the generated seed commit (#446).
_seed_commit() {
  local msg="chore(${ISSUE_REF}): start #${ISSUE_NUMBER} — ${CLEAN_TITLE}"
  # Honor the recorded commit.coauthor preference (#888) — the seed is the first
  # commit on the branch, so an opted-in project expects the trailer here too.
  if [ "$(coauthor)" = "true" ]; then
    msg="${msg}

Co-Authored-By: Claude <noreply@anthropic.com>"
  fi
  SEED_COMMIT=$(git commit-tree "HEAD^{tree}" -p HEAD -m "$msg")
  git reset --soft "$SEED_COMMIT"
}

# Judge "level with the base" against the REMOTE base when we have it — GitHub
# decides "No commits between" against ITS base ref, not the local one. A
# branch cut from origin/<base> while the local <base> lags behind has
# local-only commits (rev-list <base>..HEAD non-empty) yet is identical to the
# remote base, so the seed was skipped and PR creation still failed (#633).
_seed_base() {
  if git show-ref --verify --quiet "refs/remotes/origin/$BASE_BRANCH"; then
    echo "origin/$BASE_BRANCH"
  else
    echo "$BASE_BRANCH"
  fi
}

BASE_BRANCH=$(base_branch)
if [ -z "$(git rev-list "$(_seed_base)..HEAD" 2>/dev/null || true)" ]; then
  _seed_commit
fi

# --- Push and set upstream (retry + remote-SHA verification) ---
"$SCRIPT_DIR/push-branch.sh" "$BRANCH"

# --- Open draft PR ---
# Conventional Commits with issue key as scope (ADR-006)
PR_TITLE="${TYPE}(${ISSUE_REF}): ${CLEAN_TITLE}"
PR_BODY="Closes #${ISSUE_NUMBER}"

# --head is explicit rather than inferred. Without it `gh pr create` resolves the
# head branch through the local remote-tracking ref, so it aborts with "you must
# first push the current branch to a remote, or use the --head flag" on any clone
# whose fetch refspec is narrower than +refs/heads/*: a --single-branch or
# --depth clone configures only
# `remote.origin.fetch = +refs/heads/main:refs/remotes/origin/main`, and no
# tracking ref is ever created for the branch we just pushed. The push succeeded
# and the branch exists on the remote — only gh's inference fails. We know the
# branch name here, so there is nothing to infer.
_create_pr() {
  gh pr create \
    --draft \
    --base "$BASE_BRANCH" \
    --head "$BRANCH" \
    --title "$PR_TITLE" \
    --body "$PR_BODY"
}

PR_ERR=$(mktemp)
if ! PR_URL=$(_create_pr 2>"$PR_ERR"); then
  if grep -qi "No commits between" "$PR_ERR"; then
    # Whatever the local refs said, GitHub judged the branch empty — the seed
    # heuristic above can never cover every ref-state (#633). Seed now, push,
    # and retry once so the script keeps its one-issue-one-branch-one-PR
    # promise instead of stranding a branch without a PR.
    echo "GitHub rejected the PR (no commits) — seeding a bootstrap commit and retrying."
    _seed_commit
    "$SCRIPT_DIR/push-branch.sh" "$BRANCH"
    if ! PR_URL=$(_create_pr 2>"$PR_ERR"); then
      echo "ERROR: could not create the draft PR after seeding:" >&2
      sed 's/^/  /' "$PR_ERR" >&2
      rm -f "$PR_ERR"
      exit 1
    fi
  else
    echo "ERROR: could not create the draft PR:" >&2
    sed 's/^/  /' "$PR_ERR" >&2
    rm -f "$PR_ERR"
    exit 1
  fi
fi
rm -f "$PR_ERR"

echo "Draft PR: $PR_URL"

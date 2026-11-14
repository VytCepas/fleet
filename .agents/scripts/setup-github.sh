#!/usr/bin/env bash
# Configure or check GitHub repository governance for the scaffolded workflow.
#
# Usage: setup-github.sh [branch] [--protect]
#   --protect  apply baseline branch protection to the default branch
#              (require CI green, require PR review, block force-push).
#              Idempotent: the PUT endpoint replaces the existing config.
#
# Requires: gh and admin permission on the repository.

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

BRANCH="main"
PROTECT=0
for arg in "$@"; do
  case "$arg" in
  --protect) PROTECT=1 ;;
  --*)
    echo "Unknown option: $arg" >&2
    exit 1
    ;;
  *) BRANCH="$arg" ;;
  esac
done

HOST="$(gh_host)"
if ! gh auth status -h "$HOST" >/dev/null 2>&1; then
  echo "ERROR: gh is not authenticated for $HOST. Run: gh auth login --hostname $HOST" >&2
  exit 1
fi

REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
OWNER=${REPO%/*}
NAME=${REPO#*/}
WEB_BASE="https://$HOST"

echo "Configuring GitHub governance for $REPO ($BRANCH)"
# Default endpoint: repos/$OWNER/$NAME/branches/main/protection

if [ "$PROTECT" = 1 ]; then
  # Repo merge policy: squash-only + delete-branch-on-merge. Squash keeps history
  # linear (one commit per PR) and reuses the PR title (ADR-006); deleting the head
  # branch on merge keeps the branch list clean.
  if gh api "repos/$OWNER/$NAME" -X PATCH \
    -F allow_squash_merge=true -F allow_merge_commit=false \
    -F allow_rebase_merge=false -F delete_branch_on_merge=true >/dev/null 2>&1; then
    echo "Repo merge policy: squash-only + delete-branch-on-merge"
  else
    echo "WARNING: could not set repo merge policy (admin permission?)." >&2
  fi

  PROTECTION=$(mktemp)
  trap 'rm -f "$PROTECTION"' EXIT

  # Contexts must match the bare check-run names GitHub reports — NOT
  # "<workflow> / <job name>" (PI #555). Two traps avoided here:
  #   1. CI's lint job is a matrix, so its check-runs are "Lint and test (3.11)"…
  #      not "Lint and test"; requiring the un-expanded/prefixed name never matches
  #      and leaves the branch permanently `blocked`. Instead require the single
  #      "CI gate" job (ci.yml), which `needs:` the whole matrix + secret scan —
  #      robust to matrix changes and language-agnostic.
  #   2. review/decision is a derived status only posted on review events and is
  #      unsatisfiable for a solo owner (you can't approve your own PR), so it is
  #      NOT required here — it stays the advisory signal watch-pr.sh treats it as.
  #
  # PI-715: an *approving review* is unsatisfiable for exactly the same reason,
  # and requiring one anyway was the bug. GitHub refuses self-approval, and the
  # bot reviewers (Copilot, Codex) submit COMMENTED, never APPROVED — so on a
  # solo repo reviewDecision never left REVIEW_REQUIRED and every merge became an
  # `--admin` override of a gate that enforce_admins=false was never enforcing.
  # A bypass on every PR is strictly worse than no gate: it trains the operator
  # to reach for --no-review. org keeps the requirement (real reviewers exist).
  # required_conversation_resolution stays on for EVERY profile — that is what
  # actually enforces "resolve the review comments before merging", and unlike an
  # approval, an agent can satisfy it.
  if [ "$(gh_profile)" = "org" ]; then
    REQUIRED_APPROVALS=1
  else
    REQUIRED_APPROVALS=0
  fi
  cat >"$PROTECTION" <<JSON
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "CI gate",
      "Check PR title, branch, and linked issue"
    ]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": $REQUIRED_APPROVALS,
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false,
    "require_last_push_approval": false
  },
  "restrictions": null,
  "required_conversation_resolution": true,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON

  if gh api "repos/$OWNER/$NAME/branches/$BRANCH/protection" -X PUT --input "$PROTECTION" >/dev/null; then
    echo "Branch protection applied to $BRANCH"
  else
    echo "WARNING: could not apply branch protection. Check admin permissions and repository plan." >&2
  fi

  # Repository ruleset (#251): the org profile's "hard" enforcement layer. A
  # ruleset with an empty bypass_actors binds *everyone* (owners/admins included),
  # so it cannot be admin-bypassed — unlike classic branch protection
  # (enforce_admins=false above). Applied ONLY under the org profile;
  # individual/standalone keep advisory branch protection (admin escape hatch
  # intact), per ADR-013. Forks do NOT inherit branch/tag rulesets, so the org
  # applies it directly. Feature-probe first and warn (never fail) without rulesets.
  if [ "$(gh_profile)" != "org" ]; then
    echo "Profile is not 'org' — keeping advisory branch protection only (no owner-binding ruleset)."
  elif gh api "repos/$OWNER/$NAME/rulesets" >/dev/null 2>&1; then
    RULESET=$(mktemp)
    trap 'rm -f "$PROTECTION" "$RULESET"' EXIT
    cat >"$RULESET" <<'RULESET_JSON'
{
  "name": "project-init-baseline",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "rules": [
    { "type": "non_fast_forward" },
    { "type": "deletion" },
    { "type": "pull_request", "parameters": {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": true } },
    { "type": "required_status_checks", "parameters": {
        "strict_required_status_checks_policy": true,
        "required_status_checks": [
          { "context": "CI gate" },
          { "context": "Check PR title, branch, and linked issue" } ] } }
  ],
  "bypass_actors": []
}
RULESET_JSON
    # Update-or-create, not create-only. A POST that fails because the ruleset
    # already exists left a STALE ruleset in place forever: its required checks kept
    # blocking every PR, and re-running this script — the remedy every diagnostic
    # points at — changed nothing (PI-825). Idempotence is the whole point of a
    # setup script you are told to re-run.
    RULESET_ID=$(gh api "repos/$OWNER/$NAME/rulesets" \
      --jq '.[]? | select(.name == "project-init-baseline") | .id' 2>/dev/null | head -1)
    if [ -n "$RULESET_ID" ]; then
      if gh api "repos/$OWNER/$NAME/rulesets/$RULESET_ID" -X PUT --input "$RULESET" >/dev/null 2>&1; then
        echo "Repository ruleset 'project-init-baseline' updated (id $RULESET_ID) — required checks re-synced"
      else
        echo "WARNING: could not update the existing repository ruleset (plan/permission insufficient)." >&2
      fi
    elif gh api "repos/$OWNER/$NAME/rulesets" -X POST --input "$RULESET" >/dev/null 2>&1; then
      echo "Repository ruleset 'project-init-baseline' applied (binds everyone — empty bypass)"
    else
      echo "WARNING: could not create the repository ruleset (plan/permission insufficient)." >&2
    fi
  else
    echo "Rulesets API unavailable on this host/plan — relying on branch protection only." >&2
  fi
else
  echo "Skipping branch protection (pass --protect to apply: require CI green, require review, block force-push)"
fi

if gh api "repos/$OWNER/$NAME/code-review-settings" -X PUT -f copilot_code_review_enabled=true >/dev/null 2>&1; then
  echo "Copilot code review enabled"
else
  echo "WARNING: Enable Copilot code review manually if your plan supports it:" >&2
  echo "  $WEB_BASE/$OWNER/$NAME/settings/code_review" >&2
fi

# --- GitHub Project board field provisioning ---
# Creates the single-select metadata fields used by board-automation.yml.
# Requires a token with 'project' scope (set PROJECT_TOKEN env var, or ensure
# gh auth token has project scope). Skips fields that already exist.
echo ""
echo "Provisioning GitHub Project board fields..."

# GitHub reserves the field name "Type" (its built-in issue-type field), so a
# new board's type field is "Work type"; must match TYPE_FIELD in
# board-automation.yml (test-guarded, #1034). A board that already has a custom
# "Type" single-select (created before the reservation) keeps using it.
TYPE_FIELD="Work type"
LEGACY_TYPE_FIELD="Type"

# The board's type vocabulary as name:COLOR — the one list the create mutation,
# the existing-field option sync and the manual-setup hint all read. It must
# match the TYPE_LABEL selector in board-automation.yml (test-guarded, #1016).
TYPE_OPTIONS="feature:BLUE bug:RED chore:GRAY documentation:PURPLE test:YELLOW spike:ORANGE tech-debt:PINK"
option_names() {
  local o out=""
  for o in $1; do out="${out:+$out, }${o%%:*}"; done
  printf '%s' "$out"
}

# Single source of truth for the board number (PI #556): the PROJECT_NUMBER env
# var overrides; otherwise read github_project_number from .agents/config.yaml
# (shared with board-automation.yml / create-issue.sh); default 1. Account-scoped
# numbering means a real board is rarely #1, so all three consumers must agree or
# board state silently splits across projects.
if [ -z "${PROJECT_NUMBER:-}" ]; then
  PROJECT_NUMBER=$(grep -E '^[[:space:]]*github_project_number:' "$SCRIPT_DIR/../config.yaml" 2>/dev/null |
    head -n1 | sed 's/#.*$//' | grep -oE '[0-9]+' | head -n1 || true)
fi
PROJECT_NUMBER="${PROJECT_NUMBER:-1}"

# Use PROJECT_TOKEN if set, otherwise fall through to default gh auth
if [ -n "${PROJECT_TOKEN:-}" ]; then
  export GH_TOKEN="$PROJECT_TOKEN"
fi

# Query the project board with gh's built-in gojq (-q) instead of an external
# jq, so the script has no jq dependency (PI-362). Two short round-trips on a
# one-time admin script is negligible.
#
# One owner kind per request: a query naming both user() and organization()
# always carries a NOT_FOUND error for one of them, and on any GraphQL error gh
# exits 1 and prints the raw body, ignoring -q (#1016). __OWNER__ is filled in
# with user, then organization; only a successful request's output is kept.
owner_graphql() {
  local query="$1" kind out
  shift
  for kind in user organization; do
    if out=$(gh api graphql -f query="${query//__OWNER__/$kind}" "$@" 2>/dev/null); then
      printf '%s\n' "$out"
      return 0
    fi
  done
  return 1
}

PROJECT_QUERY='
  query($owner: String!, $number: Int!) {
    __OWNER__(login: $owner) {
      projectV2(number: $number) {
        id
        fields(first: 50) { nodes { ... on ProjectV2SingleSelectField { name } } }
      }
    }
  }'

PROJECT_ID=$(owner_graphql "$PROJECT_QUERY" \
  -f owner="$OWNER" -F number="$PROJECT_NUMBER" \
  -q '(.data.user.projectV2 // .data.organization.projectV2 // {}).id // empty' || true)

if [ -z "$PROJECT_ID" ]; then
  echo "WARNING: Project #$PROJECT_NUMBER not found for $REPO." >&2
  echo "  Ensure PROJECT_TOKEN has 'project' scope, or create these fields manually:" >&2
  echo "  • Priority     — options: high, medium, low" >&2
  echo "  • Size         — options: XS, S, M, L, XL" >&2
  echo "  • Agent ready  — options: Yes, No" >&2
  echo "  • Confidence   — options: high, medium, low, unknown" >&2
  echo "  • $TYPE_FIELD    — options: $(option_names "$TYPE_OPTIONS")" >&2
  echo "  Settings: $WEB_BASE/users/$OWNER/projects/$PROJECT_NUMBER/settings/fields" >&2
else
  EXISTING_FIELDS=$(owner_graphql "$PROJECT_QUERY" \
    -f owner="$OWNER" -F number="$PROJECT_NUMBER" \
    -q '((.data.user.projectV2 // .data.organization.projectV2).fields.nodes // [])[] | .name // empty' || true)

  FIELD_QUERY='
    query($owner: String!, $number: Int!, $name: String!) {
      __OWNER__(login: $owner) { projectV2(number: $number) { field(name: $name) {
        ... on ProjectV2SingleSelectField { id options { id name color description } } } } }
    }'
  # One line, no double quotes: it is embedded verbatim in a JSON request body.
  UPDATE_OPTIONS_MUTATION='mutation($fieldId: ID!, $opts: [ProjectV2SingleSelectFieldOptionInput!]) { updateProjectV2Field(input: { fieldId: $fieldId, singleSelectOptions: $opts }) { projectV2Field { ... on ProjectV2SingleSelectField { options { id } } } } }'
  # Emits: field id / missing names / existing ids / full option list as JSON.
  # __WANT__ is replaced with the spec's options (script constants, not input).
  # shellcheck disable=SC2016 # jq program, not shell expansion
  OPTIONS_PLAN_JQ='((.data.user.projectV2 // .data.organization.projectV2 // {}).field // {}) as $f
    | [($f.options // [])[] | {id, name, color, description}] as $have
    | [[__WANT__][] | .name as $n | select(any($have[]; .name == $n) | not)] as $miss
    | "\($f.id // "")\n\([$miss[].name] | join(" "))\n\([$have[].id] | join(" "))\n\($have + $miss | tojson)"'

  # Add the spec's options that an EXISTING single-select field lacks (#1016).
  # updateProjectV2Field's singleSelectOptions overwrites the whole list, and an
  # option keeps its identity (and every item's value) only when its id is sent
  # back — GitHub GraphQL reference, ProjectV2SingleSelectFieldOptionInput.id.
  # So every existing option is re-sent with its id, and the result is checked.
  # Board options absent from the spec are kept, deliberately: never removed.
  ensure_single_select_options() {
    local field_name="$1" spec="$2" want="" o plan field_id missing old_ids opts
    for o in $spec; do
      want="${want:+$want,}{\"name\":\"${o%%:*}\",\"color\":\"${o#*:}\",\"description\":\"\"}"
    done
    plan=$(owner_graphql "$FIELD_QUERY" -f owner="$OWNER" \
      -F number="$PROJECT_NUMBER" -f name="$field_name" \
      -q "${OPTIONS_PLAN_JQ/__WANT__/$want}" || true)
    field_id=$(printf '%s\n' "$plan" | sed -n 1p)
    missing=$(printf '%s\n' "$plan" | sed -n 2p)
    old_ids=$(printf '%s\n' "$plan" | sed -n 3p)
    opts=$(printf '%s\n' "$plan" | sed -n 4p)
    if ! printf '%s' "$field_id" | grep -Eq '^[A-Za-z0-9_-]+$' || [ -z "$opts" ]; then
      echo "  WARNING: could not read the options of '$field_name' — add any missing ones manually:" >&2
      echo "    expected: $(option_names "$spec")" >&2
      echo "    $WEB_BASE/users/$OWNER/projects/$PROJECT_NUMBER/settings/fields" >&2
      return 0
    fi
    if [ -z "$missing" ]; then
      echo "  '$field_name' already has every option"
      return 0
    fi
    local body after id lost=""
    body=$(mktemp)
    printf '{"query":"%s","variables":{"fieldId":"%s","opts":%s}}' \
      "$UPDATE_OPTIONS_MUTATION" "$field_id" "$opts" >"$body"
    if ! after=$(gh api graphql --input "$body" \
      -q '[.data.updateProjectV2Field.projectV2Field.options[]?.id] | join(" ")' 2>&1); then
      rm -f "$body"
      echo "  WARNING: could not add options to '$field_name' ($missing) — add them manually:" >&2
      [ -n "$after" ] && echo "    reason: $after" >&2
      echo "    $WEB_BASE/users/$OWNER/projects/$PROJECT_NUMBER/settings/fields" >&2
      return 0
    fi
    rm -f "$body"
    for id in $old_ids; do
      case " $after " in *" $id "*) ;; *) lost="$lost $id" ;; esac
    done
    if [ -n "$lost" ]; then
      echo "  WARNING: '$field_name' option ids changed after the update:$lost" >&2
      echo "    items that used them may have lost their $field_name value — check the board." >&2
    else
      echo "  Added to '$field_name': $missing"
    fi
  }

  ensure_single_select_field() {
    local field_name="$1"
    local mutation="$2"
    local spec="${3:-}"
    if printf '%s\n' "$EXISTING_FIELDS" | grep -Fxq "$field_name"; then
      if [ -n "$spec" ]; then
        ensure_single_select_options "$field_name" "$spec"
      else
        echo "  '$field_name' already exists — skipping"
      fi
      return 0
    fi
    # Capture stderr instead of discarding it (PI #556 minor): a swallowed
    # `2>/dev/null` made a transient/first-call failure look permanent and printed
    # a bare WARNING even when the mutation actually succeeds on retry, misleading
    # the user into manual creation. Surface the real GraphQL error so the cause
    # (scope, rate-limit, transient) is visible.
    local err
    if err=$(gh api graphql -f query="$mutation" -f projectId="$PROJECT_ID" 2>&1 >/dev/null); then
      echo "  Created '$field_name'"
    else
      # Repo: $REPO  Project: #$PROJECT_NUMBER
      echo "  WARNING: could not create '$field_name' for $REPO — add it manually:" >&2
      [ -n "$err" ] && echo "    reason: $err" >&2
      echo "    $WEB_BASE/users/$OWNER/projects/$PROJECT_NUMBER/settings/fields" >&2
    fi
  }

  ensure_single_select_field "Priority" '
    mutation($projectId: ID!) {
      createProjectV2Field(input: {
        projectId: $projectId
        dataType: SINGLE_SELECT
        name: "Priority"
        singleSelectOptions: [
          { name: "high",   color: RED,    description: "" }
          { name: "medium", color: YELLOW, description: "" }
          { name: "low",    color: GRAY,   description: "" }
        ]
      }) { projectV2Field { ... on ProjectV2SingleSelectField { id } } }
    }'

  ensure_single_select_field "Size" '
    mutation($projectId: ID!) {
      createProjectV2Field(input: {
        projectId: $projectId
        dataType: SINGLE_SELECT
        name: "Size"
        singleSelectOptions: [
          { name: "XS", color: BLUE,   description: "" }
          { name: "S",  color: GREEN,  description: "" }
          { name: "M",  color: YELLOW, description: "" }
          { name: "L",  color: ORANGE, description: "" }
          { name: "XL", color: RED,    description: "" }
        ]
      }) { projectV2Field { ... on ProjectV2SingleSelectField { id } } }
    }'

  ensure_single_select_field "Agent ready" '
    mutation($projectId: ID!) {
      createProjectV2Field(input: {
        projectId: $projectId
        dataType: SINGLE_SELECT
        name: "Agent ready"
        singleSelectOptions: [
          { name: "Yes", color: GREEN, description: "" }
          { name: "No",  color: GRAY,  description: "" }
        ]
      }) { projectV2Field { ... on ProjectV2SingleSelectField { id } } }
    }'

  ensure_single_select_field "Confidence" '
    mutation($projectId: ID!) {
      createProjectV2Field(input: {
        projectId: $projectId
        dataType: SINGLE_SELECT
        name: "Confidence"
        singleSelectOptions: [
          { name: "high",    color: GREEN,  description: "" }
          { name: "medium",  color: YELLOW, description: "" }
          { name: "low",     color: ORANGE, description: "" }
          { name: "unknown", color: GRAY,   description: "" }
        ]
      }) { projectV2Field { ... on ProjectV2SingleSelectField { id } } }
    }'

  TYPE_OPTIONS_GRAPHQL=""
  for o in $TYPE_OPTIONS; do
    TYPE_OPTIONS_GRAPHQL="$TYPE_OPTIONS_GRAPHQL
          { name: \"${o%%:*}\", color: ${o#*:}, description: \"\" }"
  done
  if printf '%s\n' "$EXISTING_FIELDS" | grep -Fxq "$LEGACY_TYPE_FIELD" &&
    ! printf '%s\n' "$EXISTING_FIELDS" | grep -Fxq "$TYPE_FIELD"; then
    echo "  Using the board's existing '$LEGACY_TYPE_FIELD' field as its type field"
    TYPE_FIELD="$LEGACY_TYPE_FIELD"
  fi
  ensure_single_select_field "$TYPE_FIELD" "
    mutation(\$projectId: ID!) {
      createProjectV2Field(input: {
        projectId: \$projectId
        dataType: SINGLE_SELECT
        name: \"$TYPE_FIELD\"
        singleSelectOptions: [$TYPE_OPTIONS_GRAPHQL
        ]
      }) { projectV2Field { ... on ProjectV2SingleSelectField { id } } }
    }" "$TYPE_OPTIONS"
fi

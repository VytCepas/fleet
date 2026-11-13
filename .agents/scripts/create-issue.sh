#!/usr/bin/env bash
# Create a GitHub issue with typed labels and planning metadata.
# Priority, Size, Agent ready, and Confidence are written into the issue body
# (so the issue is self-contained) and mirrored to the GitHub Project v2 board.
#
# Usage:
#   .agents/scripts/create-issue.sh <type> "Short description" [metadata flags]
#
# Types: feat  fix  chore  docs  test
#
# Prints the created issue number to stdout so it can be piped:
#   .agents/scripts/create-issue.sh feat "Add OAuth login" --priority high | xargs -I{} .agents/scripts/start-issue.sh {} feat

set -euo pipefail

# This script hard-requires the GitHub CLI for the actual issue creation, but
# `--help` and argument validation must work without it — otherwise you cannot
# discover the flags on a machine that has not installed gh yet (PI-362,
# 2026-07 review). require_gh is therefore called after validation, just before
# the first real gh invocation.
require_gh() {
  command -v gh >/dev/null 2>&1 || {
    echo "error: GitHub CLI (gh) not found — install: https://cli.github.com" >&2
    exit 1
  }
}

# Resolve the Python interpreter through the canonical helper (PI-361).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PY="$SCRIPT_DIR/../hooks/python-resolve.sh"

# Single source of truth for the GitHub Project board number (PI #556):
# the PROJECT_NUMBER env var overrides; otherwise read github_project_number
# from .agents/config.yaml (shared with board-automation.yml / setup-github.sh);
# default 1. Account-scoped numbering means a real board is rarely #1, so the
# three consumers must agree or board state silently splits across projects.
resolve_project_number() {
  if [ -n "${PROJECT_NUMBER:-}" ]; then
    printf '%s\n' "$PROJECT_NUMBER"
    return
  fi
  local configured=""
  configured=$(grep -E '^[[:space:]]*github_project_number:' "$SCRIPT_DIR/../config.yaml" 2>/dev/null |
    head -n1 | sed 's/#.*$//' | grep -oE '[0-9]+' | head -n1 || true)
  printf '%s\n' "${configured:-1}"
}

VALID_TYPES="feat fix chore docs test"
VALID_SCALES="epic task"
VALID_PRIORITIES="high medium low"
VALID_SIZES="XS S M L XL"
VALID_CONFIDENCES="high medium low unknown"
VALID_AGENT_READY="Yes No"

usage() {
  cat <<'EOF'
Usage: create-issue.sh <type> "Short description" [options]

Types:
  feat  fix  chore  docs  test

Options:
  --priority high|medium|low           Set Priority (issue body + project board)
  --size XS|S|M|L|XL                   Set Size (issue body + project board)
  --agent-ready Yes|No                 Set Agent ready (issue body + project board)
  --confidence high|medium|low|unknown Set Confidence (issue body + project board)
  --area VALUE                         Record affected area in body metadata
  --scale epic|task                    Mark as epic (parent) or task (leaf); adds scale label
  --parent VALUE                       Link new issue as sub-issue of VALUE
                                       Formats: 42, #42, owner/repo#42, or full issue URL
  --reference VALUE                    Add a reference; repeatable
  --dependency VALUE                   Add a dependency; repeatable
  --acceptance VALUE                   Add an acceptance criterion; repeatable
  --assignee USER                      Assign the issue
  --milestone NAME                     Set milestone by name
  --body-file FILE                     Append extra markdown body content
  -h, --help                           Show this help

Sub-issues:
  --parent links the new issue as a native GitHub sub-issue of the given parent.
  Cross-repo parents use owner/repo#42 or the full issue URL.
  --scale epic marks this issue as a parent work item (adds scale:epic label).
  Keep an epic's child tickets --scale task and sized S/M (avoid L/XL) so each is
  one small PR — small tickets keep AI-assisted work and context bounded.

Metadata model:
  GitHub labels: type and scale when labels exist or can be created.
  Markdown body (self-contained source of truth): priority, size, agent-ready,
  confidence, area, scale, parent, references, dependencies, acceptance criteria,
  Definition of Ready, and Definition of Done.
  GitHub Project fields: priority, size, agent-ready, and confidence are also
  mirrored to the board via GraphQL so they stay sortable/filterable for humans.

Missing label fallback:
  If a label is missing and cannot be created, issue creation continues without
  that label because the same metadata is still stored in the markdown body.
EOF
}

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  usage
  exit 0
fi

if [ $# -lt 2 ]; then
  usage >&2
  exit 1
fi

TYPE="$1"
DESCRIPTION="$2"
shift 2

PRIORITY=""
SIZE=""
AGENT_READY=""
CONFIDENCE=""
AREA=""
SCALE=""
PARENT=""
ASSIGNEE=""
MILESTONE=""
BODY_FILE=""
REFERENCES=()
DEPENDENCIES=()
ACCEPTANCE=()

contains_word() {
  local needle="$1"
  local haystack="$2"
  # Words are one per line so the needle is matched fixed and whole (-Fx --):
  # a value starting with '-' can't be parsed as a grep option (PI-691).
  printf '%s\n' $haystack | grep -qFx -- "$needle"
}

require_option_value() {
  local option="$1"
  local value="${2:-}"
  if [ -z "$value" ]; then
    echo "ERROR: missing value for '$option'" >&2
    usage >&2
    exit 1
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
  --priority)
    require_option_value "$1" "${2:-}"
    PRIORITY="$2"
    shift 2
    ;;
  --size)
    require_option_value "$1" "${2:-}"
    SIZE="$2"
    shift 2
    ;;
  --agent-ready)
    require_option_value "$1" "${2:-}"
    AGENT_READY="$2"
    shift 2
    ;;
  --confidence)
    require_option_value "$1" "${2:-}"
    CONFIDENCE="$2"
    shift 2
    ;;
  --area)
    require_option_value "$1" "${2:-}"
    AREA="$2"
    shift 2
    ;;
  --scale)
    require_option_value "$1" "${2:-}"
    SCALE="$2"
    shift 2
    ;;
  --parent)
    require_option_value "$1" "${2:-}"
    PARENT="$2"
    shift 2
    ;;
  --reference)
    require_option_value "$1" "${2:-}"
    REFERENCES+=("$2")
    shift 2
    ;;
  --dependency)
    require_option_value "$1" "${2:-}"
    DEPENDENCIES+=("$2")
    shift 2
    ;;
  --acceptance)
    require_option_value "$1" "${2:-}"
    ACCEPTANCE+=("$2")
    shift 2
    ;;
  --assignee)
    require_option_value "$1" "${2:-}"
    ASSIGNEE="$2"
    shift 2
    ;;
  --milestone)
    require_option_value "$1" "${2:-}"
    MILESTONE="$2"
    shift 2
    ;;
  --body-file)
    require_option_value "$1" "${2:-}"
    BODY_FILE="$2"
    shift 2
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    echo "ERROR: unknown option '$1'" >&2
    usage >&2
    exit 1
    ;;
  esac
done

if ! contains_word "$TYPE" "$VALID_TYPES"; then
  echo "ERROR: invalid type '$TYPE'. Valid types: $VALID_TYPES" >&2
  exit 1
fi

if [ -z "$DESCRIPTION" ]; then
  echo "ERROR: description cannot be empty" >&2
  exit 1
fi

if [ -n "$PRIORITY" ] && ! contains_word "$PRIORITY" "$VALID_PRIORITIES"; then
  echo "ERROR: invalid priority '$PRIORITY'. Valid: $VALID_PRIORITIES" >&2
  exit 1
fi

if [ -n "$SIZE" ] && ! contains_word "$SIZE" "$VALID_SIZES"; then
  echo "ERROR: invalid size '$SIZE'. Valid: $VALID_SIZES" >&2
  exit 1
fi

if [ -n "$CONFIDENCE" ] && ! contains_word "$CONFIDENCE" "$VALID_CONFIDENCES"; then
  echo "ERROR: invalid confidence '$CONFIDENCE'. Valid: $VALID_CONFIDENCES" >&2
  exit 1
fi

if [ -n "$SCALE" ] && ! contains_word "$SCALE" "$VALID_SCALES"; then
  echo "ERROR: invalid scale '$SCALE'. Valid scales: $VALID_SCALES" >&2
  exit 1
fi

if [ -n "$AGENT_READY" ] && ! contains_word "$AGENT_READY" "$VALID_AGENT_READY"; then
  echo "ERROR: invalid agent-ready '$AGENT_READY'. Valid: $VALID_AGENT_READY" >&2
  exit 1
fi

if [ -n "$BODY_FILE" ] && [ ! -f "$BODY_FILE" ]; then
  echo "ERROR: body file not found: $BODY_FILE" >&2
  exit 1
fi

# Arguments are valid; everything below talks to GitHub, so gh is required now.
require_gh

# ---------------------------------------------------------------------------
# Parse --parent reference into owner / repo / number.
# Accepts: 42, #42, owner/repo#42, or full GitHub issue URL.
# Sets globals PARENT_OWNER, PARENT_REPO, PARENT_NUMBER.
# ---------------------------------------------------------------------------
PARENT_OWNER=""
PARENT_REPO=""
PARENT_NUMBER=""

parse_parent() {
  local raw="${1#\#}" # strip leading #
  # Host-aware URL match: github.com, GHES, and *.ghe.com issue URLs all
  # parse (PI-691), but the URL's host must equal the active host resolved by
  # gh-host.sh — the sub-issue API call runs in the current gh context, so a
  # same-named owner/repo#n on ANOTHER host would silently link the wrong
  # parent (Codex review). Mismatch fails loudly here instead.
  if [[ "$raw" =~ ^https?://([^/]+)/([^/]+)/([^/]+)/issues/([0-9]+)$ ]]; then
    local url_host="${BASH_REMATCH[1]}" active_host
    # shellcheck source=/dev/null
    . "$SCRIPT_DIR/gh-host.sh"
    active_host="$(gh_host)"
    if [ "$url_host" != "$active_host" ]; then
      echo "ERROR: parent URL host '$url_host' does not match the active GitHub host '$active_host'." >&2
      echo "Use owner/repo#n for same-host parents, or set PROJECT_INIT_HOST/GH_HOST to '$url_host'." >&2
      exit 1
    fi
    PARENT_OWNER="${BASH_REMATCH[2]}"
    PARENT_REPO="${BASH_REMATCH[3]}"
    PARENT_NUMBER="${BASH_REMATCH[4]}"
  elif [[ "$raw" =~ ^([^/#]+)/([^#]+)#([0-9]+)$ ]]; then
    PARENT_OWNER="${BASH_REMATCH[1]}"
    PARENT_REPO="${BASH_REMATCH[2]}"
    PARENT_NUMBER="${BASH_REMATCH[3]}"
  elif [[ "$raw" =~ ^[0-9]+$ ]]; then
    PARENT_OWNER=$(gh repo view --json owner -q .owner.login)
    PARENT_REPO=$(gh repo view --json name -q .name)
    PARENT_NUMBER="$raw"
  else
    echo "ERROR: cannot parse parent '$1'. Use: 42, #42, owner/repo#42, or full URL" >&2
    exit 1
  fi
}

if [ -n "$PARENT" ]; then
  parse_parent "$PARENT"
fi

case "$TYPE" in
feat) TYPE_LABEL="feature" ;;
fix) TYPE_LABEL="bug" ;;
chore) TYPE_LABEL="chore" ;;
docs) TYPE_LABEL="documentation" ;;
test) TYPE_LABEL="test" ;;
esac

TITLE="$DESCRIPTION"

BODY_PATH=$(mktemp)
trap 'rm -f "$BODY_PATH"' EXIT

write_list() {
  local fallback="$1"
  shift
  if [ "$#" -eq 0 ]; then
    echo "- [ ] $fallback"
    return
  fi
  for item in "$@"; do
    [ -n "$item" ] && echo "- [ ] $item"
  done
}

write_bullets() {
  local fallback="$1"
  shift
  if [ "$#" -eq 0 ]; then
    echo "- $fallback"
    return
  fi
  for item in "$@"; do
    [ -n "$item" ] && echo "- $item"
  done
}

{
  echo "## Summary"
  echo
  echo "$DESCRIPTION"
  echo
  echo "## Metadata"
  echo
  echo "- Type: $TYPE"
  # PI-201: planning fields live in the body (so issue-validation.yml and the
  # issue forms agree, and an agent reading the issue needs no board query);
  # they are ALSO mirrored to the project board below for human sorting/filter.
  [ -n "$PRIORITY" ] && echo "- Priority: $PRIORITY"
  [ -n "$SIZE" ] && echo "- Size: $SIZE"
  [ -n "$AGENT_READY" ] && echo "- Agent ready: $AGENT_READY"
  [ -n "$CONFIDENCE" ] && echo "- Confidence: $CONFIDENCE"
  [ -n "$SCALE" ] && echo "- Scale: $SCALE"
  [ -n "$AREA" ] && echo "- Area: $AREA"
  if [ -n "$PARENT" ]; then
    echo "- Parent: $PARENT_OWNER/$PARENT_REPO#$PARENT_NUMBER"
  fi
  if [ -n "$ASSIGNEE" ]; then
    echo "- Assignee: @$ASSIGNEE"
  fi
  if [ -n "$MILESTONE" ]; then
    echo "- Milestone: $MILESTONE"
  fi
  # Always emit these sections (PI-201): issue-validation.yml requires them in
  # the body; "None" satisfies the check when the agent passed nothing.
  echo
  echo "## References"
  echo
  if [ "${#REFERENCES[@]}" -gt 0 ]; then
    write_bullets "None" "${REFERENCES[@]}"
  else
    echo "- None"
  fi
  echo
  echo "## Dependencies"
  echo
  if [ "${#DEPENDENCIES[@]}" -gt 0 ]; then
    write_bullets "None" "${DEPENDENCIES[@]}"
  else
    echo "- None"
  fi
  echo
  echo "## Acceptance criteria"
  echo
  # ${arr[@]+...} guard: expanding an empty array under `set -u` is fatal on
  # bash < 4.4 (macOS ships 3.2), so a run without --acceptance would die here.
  write_list "Define acceptance criteria before implementation" ${ACCEPTANCE[@]+"${ACCEPTANCE[@]}"}
  echo
  echo "## Definition of Ready"
  echo
  echo "- [ ] Acceptance criteria are clear enough to verify"
  echo "- [ ] Dependencies, references, and affected areas are recorded"
  echo
  echo "## Definition of Done"
  echo
  echo "- [ ] Implementation is complete"
  echo "- [ ] Relevant checks, tests, or manual validation are documented"
} >"$BODY_PATH"

if [ -n "$BODY_FILE" ]; then
  {
    echo
    echo "## Additional context"
    echo
    cat "$BODY_FILE"
  } >>"$BODY_PATH"
fi

ensure_label() {
  local name="$1"
  local color="$2"
  local description="$3"
  if gh label list --search "$name" --json name -q '.[].name' 2>/dev/null | grep -Fxq "$name"; then
    echo "$name"
    return
  fi
  if gh label create "$name" --color "$color" --description "$description" >/dev/null 2>&1; then
    echo "$name"
    return
  fi
  echo "Warning: label missing and could not be created: $name" >&2
}

LABEL_ARGS=()
if LABEL=$(ensure_label "$TYPE_LABEL" "0075ca" "Issue type"); then
  [ -n "$LABEL" ] && LABEL_ARGS+=(--label "$LABEL")
fi
if [ -n "$SCALE" ]; then
  if LABEL=$(ensure_label "scale:$SCALE" "f9d0c4" "Issue scale"); then
    [ -n "$LABEL" ] && LABEL_ARGS+=(--label "$LABEL")
  fi
fi

CREATE_ARGS=(--title "$TITLE" --body-file "$BODY_PATH")
if [ -n "$ASSIGNEE" ]; then
  CREATE_ARGS+=(--assignee "$ASSIGNEE")
fi
if [ -n "$MILESTONE" ]; then
  CREATE_ARGS+=(--milestone "$MILESTONE")
fi

# LABEL_ARGS stays empty when the label is missing and cannot be created (the
# documented continue-without-label fallback) — guard the expansion so that
# path survives `set -u` on bash < 4.4 (macOS ships 3.2).
ISSUE_URL=$(gh issue create "${CREATE_ARGS[@]}" ${LABEL_ARGS[@]+"${LABEL_ARGS[@]}"})
ISSUE_NUMBER=$(echo "$ISSUE_URL" | grep -oE '[0-9]+$')

# ---------------------------------------------------------------------------
# Mirror Priority / Size / Agent ready / Confidence to the GitHub Project v2
# board. They are also written into the issue body above (PI-201) so the issue
# is self-contained and passes issue-validation.yml; the board copy makes them
# sortable/filterable for humans.
# Board number resolves via resolve_project_number() (config.yaml SSOT, #556).
# ---------------------------------------------------------------------------
sync_project_fields() {
  local issue_num="$1"
  local project_num owner repo_name project_data project_id item_id

  project_num="$(resolve_project_number)"
  owner=$(gh repo view --json owner -q .owner.login)
  repo_name=$(gh repo view --json name -q .name)

  project_data=$(gh api graphql -f query='
    query($owner: String!, $number: Int!) {
      user(login: $owner) {
        projectV2(number: $number) {
          id
          fields(first: 50) {
            nodes {
              ... on ProjectV2SingleSelectField {
                id name options { id name }
              }
            }
          }
          items(first: 100) {
            nodes { id content { ... on Issue { number } } }
          }
        }
      }
      organization(login: $owner) {
        projectV2(number: $number) {
          id
          fields(first: 50) {
            nodes {
              ... on ProjectV2SingleSelectField {
                id name options { id name }
              }
            }
          }
          items(first: 100) {
            nodes { id content { ... on Issue { number } } }
          }
        }
      }
    }' -f owner="$owner" -F number="$project_num" 2>/dev/null) || true
  [ -z "$project_data" ] && project_data='{}'

  # Write project data to a temp file to avoid single-quote issues in shell args
  local pdata_file
  pdata_file=$(mktemp)
  printf '%s' "$project_data" >"$pdata_file"

  project_id=$(
    "$PY" - "$pdata_file" <<'PYEOF' 2>/dev/null || true
import sys, json
d = json.load(open(sys.argv[1])).get('data', {})
p = (d.get('user') or d.get('organization') or {}).get('projectV2') or {}
print(p.get('id', ''))
PYEOF
  )

  if [ -z "$project_id" ]; then
    rm -f "$pdata_file"
    echo "Warning: project #$project_num not found — skipping field sync (set PROJECT_NUMBER if needed)" >&2
    return 0
  fi

  item_id=$(
    "$PY" - "$pdata_file" "$issue_num" <<'PYEOF' 2>/dev/null || true
import sys, json
d = json.load(open(sys.argv[1])).get('data', {})
p = (d.get('user') or d.get('organization') or {}).get('projectV2') or {}
for item in (p.get('items') or {}).get('nodes', []):
    if (item.get('content') or {}).get('number') == int(sys.argv[2]):
        print(item['id'])
        break
PYEOF
  )

  if [ -z "$item_id" ]; then
    local issue_node_id
    issue_node_id=$(gh api "repos/$owner/$repo_name/issues/$issue_num" --jq '.node_id' 2>/dev/null || true)
    if [ -n "$issue_node_id" ]; then
      item_id=$(gh api graphql -f query='
        mutation($project: ID!, $content: ID!) {
          addProjectV2ItemById(input: { projectId: $project, contentId: $content }) {
            item { id }
          }
        }' -f project="$project_id" -f content="$issue_node_id" \
        --jq '.data.addProjectV2ItemById.item.id' 2>/dev/null || true)
    fi
  fi

  if [ -z "$item_id" ]; then
    rm -f "$pdata_file"
    echo "Warning: could not add #$issue_num to project #$project_num — skipping field sync" >&2
    return 0
  fi

  set_field() {
    local field_name="$1" option_name="$2"
    [ -n "$option_name" ] || return 0

    local field_id option_id
    field_id=$(
      "$PY" - "$pdata_file" "$field_name" <<'PYEOF' 2>/dev/null || true
import sys, json
d = json.load(open(sys.argv[1])).get('data', {})
p = (d.get('user') or d.get('organization') or {}).get('projectV2') or {}
for f in (p.get('fields') or {}).get('nodes', []):
    if f.get('name') == sys.argv[2]:
        print(f.get('id', ''))
        break
PYEOF
    )

    if [ -z "$field_id" ]; then
      echo "Warning: project field '$field_name' not found — skipping" >&2
      return 0
    fi

    option_id=$(
      "$PY" - "$pdata_file" "$field_name" "$option_name" <<'PYEOF' 2>/dev/null || true
import sys, json
d = json.load(open(sys.argv[1])).get('data', {})
p = (d.get('user') or d.get('organization') or {}).get('projectV2') or {}
for f in (p.get('fields') or {}).get('nodes', []):
    if f.get('name') == sys.argv[2]:
        for opt in f.get('options', []):
            if opt.get('name') == sys.argv[3]:
                print(opt['id'])
                break
        break
PYEOF
    )

    if [ -z "$option_id" ]; then
      echo "Warning: option '$option_name' not found in field '$field_name' — skipping" >&2
      return 0
    fi

    gh api graphql -f query='
      mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
        updateProjectV2ItemFieldValue(input: {
          projectId: $project
          itemId: $item
          fieldId: $field
          value: { singleSelectOptionId: $option }
        }) { projectV2Item { id } }
      }' \
      -f project="$project_id" -f item="$item_id" \
      -f field="$field_id" -f option="$option_id" >/dev/null 2>&1 ||
      echo "Warning: failed to set '$field_name'='$option_name'" >&2
  }

  [ -n "$PRIORITY" ] && set_field "Priority" "$PRIORITY"
  [ -n "$SIZE" ] && set_field "Size" "$SIZE"
  [ -n "$AGENT_READY" ] && set_field "Agent ready" "$AGENT_READY"
  [ -n "$CONFIDENCE" ] && set_field "Confidence" "$CONFIDENCE"
  rm -f "$pdata_file"
}

if [ -n "$PRIORITY" ] || [ -n "$SIZE" ] || [ -n "$AGENT_READY" ] || [ -n "$CONFIDENCE" ]; then
  sync_project_fields "$ISSUE_NUMBER"
fi

# ---------------------------------------------------------------------------
# Link as a native GitHub sub-issue when --parent was specified.
# Uses the addSubIssue GraphQL mutation (supports cross-repo parents via URL).
# ---------------------------------------------------------------------------
if [ -n "$PARENT" ]; then
  PARENT_NODE_ID=$(gh api graphql -f query='
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        issue(number: $number) { id }
      }
    }' \
    -f owner="$PARENT_OWNER" \
    -f repo="$PARENT_REPO" \
    -F number="$PARENT_NUMBER" \
    --jq '.data.repository.issue.id')

  CHILD_NODE_ID=$(gh api "repos/:owner/:repo/issues/$ISSUE_NUMBER" --jq '.node_id')

  gh api graphql -f query='
    mutation($parent: ID!, $child: ID!) {
      addSubIssue(input: { issueId: $parent, subIssueId: $child }) {
        issue { number }
        subIssue { number }
      }
    }' \
    -f parent="$PARENT_NODE_ID" \
    -f child="$CHILD_NODE_ID" >/dev/null

  echo "Linked #$ISSUE_NUMBER as sub-issue of $PARENT_OWNER/$PARENT_REPO#$PARENT_NUMBER" >&2
fi

echo "$ISSUE_NUMBER"

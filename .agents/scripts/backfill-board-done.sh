#!/usr/bin/env bash
# One-time backfill: move every CLOSED issue's board card to "Done".
#
# Why this exists: board-automation.yml historically set Status=Done ONLY when a
# PR with "Closes #N" merged. Issues closed any other way — manually, as a
# duplicate, as "not planned", by a PR whose body didn't match, or an epic closed
# once its children finished — stayed frozen in their previous column (Backlog /
# To Do / In Progress / In Review). The workflow now also listens for
# `issues: closed`, but that only fixes issues closed FROM NOW ON. This script
# reconciles the already-closed backlog in a single pass.
#
# Usage:
#   .agents/scripts/backfill-board-done.sh [--dry-run] [--done-name "Done"]
#
#   --dry-run      list the cards that WOULD move; change nothing.
#   --done-name    name of the target Status option (default: "Done").
#
# Requires: gh authenticated with a token carrying the 'project' scope
# (PROJECT_TOKEN, or a gh auth token with project scope). The board number comes
# from PROJECT_NUMBER or .agents/config.yaml — the same source of truth as
# setup-github.sh / create-issue.sh / board-automation.yml.

set -euo pipefail

command -v gh >/dev/null 2>&1 || {
  echo "error: GitHub CLI (gh) not found — install: https://cli.github.com" >&2
  exit 1
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PY="$SCRIPT_DIR/../hooks/python-resolve.sh"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/gh-host.sh"

DRY_RUN=0
DONE_NAME="Done"
while [ $# -gt 0 ]; do
  case "$1" in
  --dry-run) DRY_RUN=1 ;;
  --done-name)
    DONE_NAME="${2:-}"
    [ -n "$DONE_NAME" ] || {
      echo "error: --done-name needs a value" >&2
      exit 1
    }
    shift
    ;;
  -h | --help)
    sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  *)
    echo "error: unknown option '$1'" >&2
    exit 1
    ;;
  esac
  shift
done

HOST="$(gh_host)"
if ! gh auth status -h "$HOST" >/dev/null 2>&1; then
  echo "ERROR: gh is not authenticated for $HOST. Run: gh auth login --hostname $HOST" >&2
  exit 1
fi

if [ -n "${PROJECT_TOKEN:-}" ]; then
  export GH_TOKEN="$PROJECT_TOKEN"
fi

OWNER=$(gh repo view --json owner -q .owner.login)

# Board number: PROJECT_NUMBER env overrides; else read github_project_number
# from .agents/config.yaml (shared SSOT, #556); default 1.
if [ -z "${PROJECT_NUMBER:-}" ]; then
  PROJECT_NUMBER=$(grep -E '^[[:space:]]*github_project_number:' "$SCRIPT_DIR/../config.yaml" 2>/dev/null |
    head -n1 | sed 's/#.*$//' | grep -oE '[0-9]+' | head -n1 || true)
fi
PROJECT_NUMBER="${PROJECT_NUMBER:-1}"

echo "Backfilling GitHub Project #$PROJECT_NUMBER for $OWNER (target status: '$DONE_NAME')"

# --- Resolve project id, Status field id, and the target option id. ---
# user(...) errors on an org-owned board and vice versa; tolerate it so the jq/py
# fallback selects whichever path returned data (same tactic as board-automation.yml).
META_QUERY='
  query($owner: String!, $number: Int!) {
    user(login: $owner) {
      projectV2(number: $number) {
        id
        field(name: "Status") {
          ... on ProjectV2SingleSelectField { id options { id name } }
        }
      }
    }
    organization(login: $owner) {
      projectV2(number: $number) {
        id
        field(name: "Status") {
          ... on ProjectV2SingleSelectField { id options { id name } }
        }
      }
    }
  }'

META=$(gh api graphql -f query="$META_QUERY" -f owner="$OWNER" -F number="$PROJECT_NUMBER" 2>/dev/null || true)
META_FILE=$(mktemp)
trap 'rm -f "$META_FILE"' EXIT
printf '%s' "$META" >"$META_FILE"

META_INFO=$(
  "$PY" - "$META_FILE" "$DONE_NAME" <<'PYEOF'
import sys, json
d = (json.load(open(sys.argv[1])) or {}).get("data") or {}
p = (d.get("user") or d.get("organization") or {}).get("projectV2") or {}
f = p.get("field") or {}
done = ""
for o in f.get("options") or []:
    if o.get("name") == sys.argv[2]:
        done = o["id"]
        break
print(p.get("id", ""), f.get("id", ""), done)
PYEOF
)
read -r PROJECT_ID FIELD_ID DONE_OPTION_ID <<EOF
$META_INFO
EOF

if [ -z "$PROJECT_ID" ]; then
  echo "ERROR: project #$PROJECT_NUMBER not found (or PROJECT_TOKEN lacks 'project' scope)." >&2
  exit 1
fi
if [ -z "$FIELD_ID" ]; then
  echo "ERROR: the board has no single-select 'Status' field — nothing to backfill." >&2
  exit 1
fi
if [ -z "$DONE_OPTION_ID" ]; then
  echo "ERROR: Status field has no option named '$DONE_NAME'. Pass --done-name to match your board." >&2
  exit 1
fi

# --- Walk every board item (paginated past the 100-item cap) and update each
# card whose linked issue is CLOSED but whose Status is not already the target. ---
ITEMS_QUERY='
  query($owner: String!, $number: Int!, $after: String) {
    user(login: $owner) {
      projectV2(number: $number) {
        items(first: 100, after: $after) {
          pageInfo { hasNextPage endCursor }
          nodes {
            id
            status: fieldValueByName(name: "Status") {
              ... on ProjectV2ItemFieldSingleSelectValue { name }
            }
            content { ... on Issue { number state } }
          }
        }
      }
    }
    organization(login: $owner) {
      projectV2(number: $number) {
        items(first: 100, after: $after) {
          pageInfo { hasNextPage endCursor }
          nodes {
            id
            status: fieldValueByName(name: "Status") {
              ... on ProjectV2ItemFieldSingleSelectValue { name }
            }
            content { ... on Issue { number state } }
          }
        }
      }
    }
  }'

update_card() {
  local item_id="$1"
  gh api graphql -f query='
    mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
      updateProjectV2ItemFieldValue(input: {
        projectId: $project
        itemId: $item
        fieldId: $field
        value: { singleSelectOptionId: $option }
      }) { projectV2Item { id } }
    }' -f project="$PROJECT_ID" -f item="$item_id" \
    -f field="$FIELD_ID" -f option="$DONE_OPTION_ID" >/dev/null
}

CURSOR=""
MOVED=0
SCANNED=0
PAGE_FILE=$(mktemp)
trap 'rm -f "$META_FILE" "$PAGE_FILE"' EXIT

while :; do
  if [ -n "$CURSOR" ]; then
    PAGE=$(gh api graphql -f query="$ITEMS_QUERY" -f owner="$OWNER" \
      -F number="$PROJECT_NUMBER" -f after="$CURSOR" 2>/dev/null || true)
  else
    PAGE=$(gh api graphql -f query="$ITEMS_QUERY" -f owner="$OWNER" \
      -F number="$PROJECT_NUMBER" 2>/dev/null || true)
  fi
  [ -n "$PAGE" ] || {
    echo "ERROR: failed to read a page of board items." >&2
    exit 1
  }
  printf '%s' "$PAGE" >"$PAGE_FILE"

  # First stdout line: "<hasNextPage> <endCursor>". Remaining lines:
  # "<itemId> <issueNumber>" for each CLOSED card not already at the target.
  PARSED=$(
    "$PY" - "$PAGE_FILE" "$DONE_NAME" <<'PYEOF'
import sys, json
d = (json.load(open(sys.argv[1])) or {}).get("data") or {}
p = (d.get("user") or d.get("organization") or {}).get("projectV2") or {}
items = p.get("items") or {}
info = items.get("pageInfo") or {}
print(("true" if info.get("hasNextPage") else "false"), info.get("endCursor") or "")
for node in items.get("nodes") or []:
    content = node.get("content") or {}
    if content.get("state") != "CLOSED":
        continue
    if ((node.get("status") or {}).get("name")) == sys.argv[2]:
        continue
    num = content.get("number")
    print(node["id"], num if num is not None else "?")
PYEOF
  )

  HEAD=$(printf '%s\n' "$PARSED" | head -n1)
  HAS_NEXT=$(printf '%s' "$HEAD" | cut -d' ' -f1)
  CURSOR=$(printf '%s' "$HEAD" | cut -d' ' -f2-)

  while read -r ITEM_ID ISSUE_NUM; do
    [ -n "$ITEM_ID" ] || continue
    SCANNED=$((SCANNED + 1))
    if [ "$DRY_RUN" = 1 ]; then
      echo "  would move #$ISSUE_NUM -> $DONE_NAME"
    else
      if update_card "$ITEM_ID"; then
        echo "  moved #$ISSUE_NUM -> $DONE_NAME"
        MOVED=$((MOVED + 1))
      else
        echo "  WARNING: failed to move #$ISSUE_NUM" >&2
      fi
    fi
  done <<EOF
$(printf '%s\n' "$PARSED" | tail -n +2)
EOF

  [ "$HAS_NEXT" = "true" ] || break
  [ -n "$CURSOR" ] || break
done

if [ "$DRY_RUN" = 1 ]; then
  echo "Dry run: $SCANNED closed card(s) would move to '$DONE_NAME'. Re-run without --dry-run to apply."
else
  echo "Done: moved $MOVED card(s) to '$DONE_NAME'."
fi

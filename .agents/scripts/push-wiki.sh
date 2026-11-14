#!/usr/bin/env bash
# push-wiki.sh — clone the GitHub wiki, update Home.md, and push.
# Usage: push-wiki.sh <repo-slug> <wiki-source-file> [--prune <page.md> ...]
#   repo-slug         e.g. owner/repo-name
#   wiki-source-file  path to the markdown file to write as Home.md
#   --prune           remove the named stale page(s) in the same commit
#
# The guard allowlists this script so it is not blocked by the git-push rule.

case "${1-}" in
-h | --help) # the header above is the help; nothing else runs (#992)
  sed -n '2,/^[^#]/s/^# \{0,1\}//p' "$0"
  exit 0
  ;;
esac
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/gh-host.sh"

if [[ $# -lt 2 ]]; then
  echo "Usage: push-wiki.sh <repo-slug> <wiki-source-file> [--prune <page.md> ...]" >&2
  exit 1
fi

REPO_SLUG="$1"
SOURCE_FILE="$2"
shift 2
PRUNE_PAGES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
  --prune)
    if [[ $# -lt 2 || "$2" == --* ]]; then
      echo "--prune requires at least one page name" >&2
      exit 1
    fi
    shift
    while [[ $# -gt 0 && "$1" != --* ]]; do
      PRUNE_PAGES+=("$1")
      shift
    done
    ;;
  *)
    echo "Unknown option: $1" >&2
    exit 1
    ;;
  esac
done
WIKI_DIR="$(mktemp -d)"
trap 'rm -rf "$WIKI_DIR"' EXIT

echo "Cloning wiki for $REPO_SLUG..."
git clone "$(gh_web_base)/${REPO_SLUG}.wiki.git" "$WIKI_DIR"

cp "$SOURCE_FILE" "$WIKI_DIR/Home.md"

cd "$WIKI_DIR"
# ${arr[@]+...} guard: expanding an empty array under `set -u` is a fatal
# "unbound variable" on bash 3.2-4.3 (stock macOS), aborting before the commit.
for page in ${PRUNE_PAGES[@]+"${PRUNE_PAGES[@]}"}; do
  if [[ -f "$page" ]]; then
    git rm -q "$page"
    echo "Pruned $page"
  fi
done
git add Home.md
if git diff --cached --quiet; then
  echo "Wiki already up to date."
  exit 0
fi
# Fall back to a bot identity so the commit succeeds on ephemeral CI/agent
# runners that have no user.name/user.email configured (per-command -c, so a
# developer's global git identity is untouched).
git \
  -c "user.name=${GIT_AUTHOR_NAME:-project-init wiki bot}" \
  -c "user.email=${GIT_AUTHOR_EMAIL:-noreply@users.noreply.github.com}" \
  commit -m "Update wiki content"
git push
echo "Wiki updated."

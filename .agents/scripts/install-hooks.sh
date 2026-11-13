#!/usr/bin/env bash
# Install git hooks from .github/hooks/ to .git/hooks/
# Run this once after cloning or when hooks are updated

case "${1-}" in
-h | --help) # the header above is the help; nothing else runs (#992)
  sed -n '2,/^[^#]/s/^# \{0,1\}//p' "$0"
  exit 0
  ;;
esac
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GIT_HOOKS_SRC="$REPO_ROOT/.github/hooks"
GIT_HOOKS_DST="$REPO_ROOT/.git/hooks"

if [ ! -d "$GIT_HOOKS_DST" ]; then
  echo "Error: .git/hooks directory not found. Are you in a git repository?"
  exit 1
fi

if [ ! -d "$GIT_HOOKS_SRC" ]; then
  echo "Warning: .github/hooks directory not found."
  exit 0
fi

echo "Installing git hooks from .github/hooks/ to .git/hooks/..."

for hook_file in "$GIT_HOOKS_SRC"/*; do
  [ -f "$hook_file" ] || continue

  hook_name=$(basename "$hook_file")
  hook_dst="$GIT_HOOKS_DST/$hook_name"

  if [ -e "$hook_dst" ] && [ ! -L "$hook_dst" ]; then
    # File exists and is not a symlink - back it up
    mv "$hook_dst" "$hook_dst.backup.$(date +%s)"
    echo "  Backed up existing $hook_name"
  fi

  # If the destination is a symlink (e.g. a hooks manager like husky), remove
  # the link first so we replace it rather than writing through `cp` to its
  # referent and clobbering a shared file outside .git/hooks (PI-204).
  [ -L "$hook_dst" ] && rm -f "$hook_dst"

  # Copy the project hook into place.
  if cp -P "$hook_file" "$hook_dst" 2>/dev/null; then
    chmod +x "$hook_dst"
  else
    echo "  ✗ Failed to install $hook_name"
    exit 1
  fi
done

# The pre-commit hook scans staged changes with gitleaks (ADR-007).
# It fails open when gitleaks is missing — CI is the hard backstop — but
# local feedback is much faster, so nudge here.
if ! command -v gitleaks >/dev/null 2>&1; then
  echo ""
  echo "NOTE: gitleaks is not installed — the pre-commit secret scan will be"
  echo "skipped locally (CI still scans). Install it for fast local feedback:"
  echo "  https://github.com/gitleaks/gitleaks#installing"
fi

echo "To reinstall hooks after pulling changes, run:"
echo "  .agents/scripts/install-hooks.sh"

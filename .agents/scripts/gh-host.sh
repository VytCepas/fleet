#!/usr/bin/env bash
# gh-host.sh — resolve the active GitHub host for host-aware links and URLs.
#
# Sourced by lifecycle scripts so manual links, clone URLs, and curl-based API
# calls work on github.com, GHE.com (data residency, *.ghe.com), and GitHub
# Enterprise Server (GHES) — not just public github.com (ADR-013, spike #254).
#
# `gh api` already targets the repo's host automatically, so these helpers are
# only needed for hardcoded URLs and non-gh callers.
#
# On Enterprise Managed Users (EMU), external forks are blocked — an org mirrors
# or imports the upstream instead (see the org fork lifecycle runbook). These
# helpers resolve the host the same way regardless of fork-vs-import.
#
# Resolution order: PROJECT_INIT_HOST → GH_HOST → current repo remote → github.com.

# Strip scheme, userinfo, path, and port from a URL or host string → bare host.
# Handles https://, http://, ssh://, scp-style git@host:owner/repo, and bare hosts.
_gh_host_normalize() {
  printf '%s\n' "$1" | sed -E 's#^[a-zA-Z][a-zA-Z0-9+.-]*://##; s#^[^@/]*@##; s#[/:].*$##'
}

gh_host() {
  local raw=""
  if [ -n "${PROJECT_INIT_HOST:-}" ]; then
    raw="$PROJECT_INIT_HOST"
  elif [ -n "${GH_HOST:-}" ]; then
    raw="$GH_HOST"
  else
    raw=$(gh repo view --json url -q .url 2>/dev/null || true)
    [ -z "$raw" ] && raw=$(git config --get remote.origin.url 2>/dev/null || true)
  fi
  local host
  host="$(_gh_host_normalize "$raw")"
  printf '%s\n' "${host:-github.com}"
}

# Web base for browser links, e.g. https://github.com or https://ghes.example.com
gh_web_base() { printf 'https://%s\n' "$(gh_host)"; }

# REST API base for curl-based callers. Prefer `gh api` where possible (it
# resolves the host itself). github.com & *.ghe.com use api.<host>; GHES uses
# <host>/api/v3. Override explicitly with PROJECT_INIT_API_BASE.
gh_api_base() {
  if [ -n "${PROJECT_INIT_API_BASE:-}" ]; then
    printf '%s\n' "$PROJECT_INIT_API_BASE"
    return
  fi
  local host
  host="$(gh_host)"
  case "$host" in
  "") printf 'https://api.github.com\n' ;;
  github.com | *.ghe.com) printf 'https://api.%s\n' "$host" ;;
  *) printf 'https://%s/api/v3\n' "$host" ;;
  esac
}

# Distribution profile recorded by project-init in .agents/config.yaml (#247);
# defaults to individual when unset. Gates org-only hard enforcement (#251).
# Anchored on this file's own location (.agents/scripts/ -> ../config.yaml),
# NOT the caller's cwd — a cwd-relative read silently resolves to the
# 'individual' default from any subdirectory, which would let watch-pr.sh
# admin-merge past org branch protection and setup-github.sh skip the org
# ruleset.
gh_profile() {
  local cfg prof=""
  cfg="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../config.yaml"
  [ -f "$cfg" ] && prof=$(sed -nE 's/^[[:space:]]*profile:[[:space:]]*([a-z]+).*/\1/p' "$cfg" | head -1)
  printf '%s\n' "${prof:-individual}"
}

# Review-fix cycles watch-pr.sh runs before it stops asking for another pass
# (#714). 0 means no review control: merge as soon as CI is green. Anchored on
# this file's location for the same reason gh_profile is. Falls back to 2 — the
# default the wizard explains — when unset or absent.
review_cycles() {
  local cfg n=""
  cfg="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../config.yaml"
  [ -f "$cfg" ] && n=$(sed -nE 's/^[[:space:]]*review_cycles:[[:space:]]*([0-9]+).*/\1/p' "$cfg" | head -1)
  printf '%s\n' "${n:-2}"
}

# Check names watch-pr.sh treats as informational — reported, never blocking
# (#837). Echoes the raw single-line JSON array from config.yaml (e.g.
# ["board-sync"]), or nothing when the key is absent/empty. Use for checks that
# are known-dead — a billing-locked GitHub-hosted job dies permanently as a
# zero-step startup failure while self-hosted CI stays green — so one dead
# check cannot deadlock every PR's merge path. Anchored on this file's
# location for the same reason gh_profile is.
monitor_ignore_checks() {
  local cfg raw=""
  cfg="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../config.yaml"
  [ -f "$cfg" ] && raw=$(sed -nE 's/^[[:space:]]*monitor_ignore_checks:[[:space:]]*(\[[^]]*\]).*/\1/p' "$cfg" | head -1)
  printf '%s\n' "$raw"
}

# Co-Authored-By: Claude trailer preference recorded in .agents/config.yaml
# (commit.coauthor, #888). Echoes "true" when the project opted in, else nothing,
# so callers can gate the trailer with [ "$(coauthor)" = "true" ]. Anchored on
# this file's location like the other readers. Absent key ⇒ off (a pre-#888
# scaffold never had the trailer).
coauthor() {
  local cfg v=""
  cfg="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../config.yaml"
  [ -f "$cfg" ] && v=$(sed -nE 's/^[[:space:]]*coauthor:[[:space:]]*(true|false).*/\1/p' "$cfg" | head -1)
  [ "$v" = "true" ] && printf 'true\n'
}

# Base branch for feature PRs. Single trunk: the scaffolder pins the rendered
# workflows (ci.yml, validate-pr.yml) to 'main', so this MUST return 'main' too —
# resolving the live default branch instead would let start-issue.sh target a
# branch the CI/validation workflows don't cover (PR #330 review). Used by
# start-issue.sh.
base_branch() {
  printf 'main\n'
}

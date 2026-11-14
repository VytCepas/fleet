#!/usr/bin/env bash
# python-resolve.sh — canonical Python-interpreter resolver (PI-361).
#
# Hooks and lifecycle scripts are stdlib-only, so any Python 3 works — they
# just need a *resolvable* interpreter. `python3` exists on macOS/Linux/WSL but
# not always on native Windows (Git Bash) or uv-only hosts, where the command
# may be `python` or only available via `uv run python`. Routing every Python
# invocation through this one file keeps that resolution in a single place.
#
# Forwards "$@" verbatim, so it handles every call form identically:
#   python-resolve.sh script.py [args]      # file form
#   python-resolve.sh -c "…"                # inline
#   python-resolve.sh - <<'PY' … PY         # heredoc on stdin
#   VAR=x python-resolve.sh -c "…"          # env-prefixed
# `python3` always means Python 3 (PEP 394), so use it directly when present.
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$@"
fi
# Bare `python` may still be Python 2 on legacy hosts — use it only if it's 3.x.
if command -v python >/dev/null 2>&1 &&
  python -c 'import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)' 2>/dev/null; then
  exec python "$@"
fi
# Last resort: uv can run a managed Python — but only if uv is actually present,
# so we fail with a clear message instead of a confusing `uv: command not found`.
if command -v uv >/dev/null 2>&1; then
  exec uv run python "$@"
fi
echo "python-resolve.sh: no Python 3 found (need python3, a Python 3 'python', or uv)" >&2
exit 127

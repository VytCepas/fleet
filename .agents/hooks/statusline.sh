#!/usr/bin/env bash
# statusline.sh — zero-token context meter (PI-661, epic #641).
# Claude Code statusLine command: receives session JSON on stdin, prints one
# line rendered in the terminal footer. Statusline output NEVER enters the
# transcript, so this surfaces context-window % and cache-hit rate at zero
# token cost. Fail-open: any parse error prints a minimal placeholder.

case "${1-}" in
-h | --help) # the header above is the help; nothing else runs (#992)
  sed -n '2,/^[^#]/s/^# \{0,1\}//p' "$0"
  exit 0
  ;;
esac
set -u

# Resolve the Python interpreter through the canonical helper (PI-361).
PY="$(dirname "$0")/python-resolve.sh"

"$PY" -c '
import json
import sys

try:
    d = json.load(sys.stdin)
    model = (d.get("model") or {}).get("display_name") or "?"
    cw = d.get("context_window") or {}
    pct = cw.get("used_percentage")
    usage = cw.get("current_usage") or {}
    reads = usage.get("cache_read_input_tokens") or 0
    total_in = (
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
        + reads
    )
    parts = [f"[{model}]"]
    if pct is not None:
        pct = int(pct)
        filled = min(10, pct // 10)
        bar = "#" * filled + "-" * (10 - filled)
        parts.append(f"ctx {bar} {pct}%")
        if pct >= 60:
            parts.append("(consider /compact)")
    else:
        parts.append("ctx: warming up")
    if total_in:
        parts.append(f"cache {100 * reads // total_in}%")
    print(" ".join(parts))
except Exception:
    print("ctx: n/a")
' 2>/dev/null || echo "ctx: n/a"

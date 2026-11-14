---
name: add-hook
description: Adds a deterministic hook that fires automatically on tool events. Use when asked to enforce a rule on every commit, block a dangerous pattern, validate output, or gate any tool call.
when_to_use: Use when the user wants to automate a check or action that runs every time a specific event occurs — e.g. "block pushes to main", "run linter before every commit", "log every Bash command".
argument-hint: "<hook-name> <event> <description>"
allowed-tools: Read Write Bash
---

> **Claude Code specific**: this skill manages Claude Code configuration
> (settings.json wiring). Other agents do not consume what it produces.


## Step 0 — Confirm the current schema (best-effort)

The event names and output schema below are a snapshot and can lag Claude Code
releases. **If** a docs-lookup tool is available to you — the Context7 MCP, or
WebFetch when it's permitted — confirm the event name / output field against the
current reference (<https://docs.agents.com/en/docs/claude-code/hooks>) before
relying on it. This skill's `allowed-tools` does not grant those tools, so skip
this step cleanly whenever the tool is unavailable, unapproved, egress is
disabled (`--no-egress` / air-gapped), or the lookup fails — fall back to the
embedded reference below. Don't request extra permissions and never block on it.

## Step 1 — Choose an event

Pick the event that matches when the hook should fire:

**Tool execution** (most common):
- `PreToolUse` — before a tool runs; output block JSON to block it
- `PostToolUse` — after a tool runs; cannot block, can log or validate
- `PostToolBatch` — after a batch of parallel tool calls completes
- `PostToolUseFailure` — after a tool fails
- `PermissionRequest` — when Claude asks for permission; can auto-approve
- `PermissionDenied` — when permission is denied; can request a retry

**Session lifecycle:**
- `SessionStart` — when a session begins
- `SessionEnd` — when a session closes
- `PreCompact` — before context compaction (can block)
- `Stop` — when Claude stops generating (end of turn)
- `StopFailure` — when a turn fails

**User input:**
- `UserPromptSubmit` — when the user submits a prompt
- `UserPromptExpansion` — when slash commands expand

**Agent / task events:**
- `SubagentStart` / `SubagentStop` — subagent lifecycle
- `TaskCreated` / `TaskCompleted` — task lifecycle (via `TaskCreate` / completion)

**File/config events:**
- `FileChanged`, `CwdChanged`, `ConfigChange`

## Step 2 — Write the hook script

Create `.agents/hooks/<name>.sh`:

```bash
#!/usr/bin/env bash
# Hook receives JSON on stdin from Claude Code.
INPUT=$(cat)

# Resolve Python through the canonical helper so the hook runs wherever the
# interpreter is `python3`, `python`, or only available via `uv run` (PI-361).
PY="$(dirname "$0")/python-resolve.sh"

# exit 0 = allow (or no-op for non-blocking events)
# PreToolUse block: stdout JSON {"hookSpecificOutput":{"hookEventName":
#   "PreToolUse","permissionDecision":"deny","permissionDecisionReason":"..."}}
# stdout JSON with {"additionalContext":"..."} = inject context
# Always exit 0 — exit 1 means hook error, not a block

# Example: block pushes to main
CMD=$(echo "$INPUT" | "$PY" -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print((data.get('tool_input', {}) or {}).get('command', '') or '')
" 2>/dev/null || true)

[ -z "$CMD" ] && exit 0

if echo "$CMD" | grep -qE 'git push.*(main|master)'; then
  "$PY" -c "import json,sys; print(json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'deny','permissionDecisionReason':sys.argv[1]}}))" \
    "Direct push to main is not allowed. Use a branch and PR."
  exit 0
fi
exit 0
```

Make it executable: `chmod +x .agents/hooks/<name>.sh`

## Step 3 — Wire it in settings.json

Add to `.agents/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "\"$CLAUDE_PROJECT_DIR\"/.agents/hooks/<name>.sh", "timeout": 10}]
      }
    ]
  }
}
```

**Matchers:** tool name (`Bash`, `Write`, `Edit`), pipe-separated (`Write|Edit`), or `*` for all tools.

## Alternative — Inline hook in a skill

Hooks can also live in a skill's frontmatter and fire only while the skill is active:

```yaml
---
name: my-skill
hooks:
  PreToolUse:
    - matcher: "Bash"
      hooks:
        - type: command
          command: "./.agents/hooks/check.sh"
---
```

## Step 4 — Test

Trigger the wired tool and verify the hook fires. Check `$CLAUDE_PROJECT_DIR` is set correctly in the command path.

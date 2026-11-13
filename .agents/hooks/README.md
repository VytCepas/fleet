# `.agents/hooks/`

Session hooks — deterministic bash or python scripts. The `settings.json` at `.agents/settings.json` wires them to Claude Code events (SessionStart, SessionEnd, PreToolUse, etc.).

Keep hooks fast, idempotent, and non-interactive.

These hooks are fast-feedback UX for Claude Code sessions — they are not the
security boundary. Secret scanning (gitleaks) and lifecycle gating run as git
hooks installed by `.agents/scripts/install-hooks.sh`, with CI as the
backstop, so they bind every agent and every human (ADR-007).

## Hook Executability Convention

- **Shell hooks** (`.sh` files): Must have the executable bit (`+x`). They run directly via `bash path/to/hook.sh`.
- **Python hooks** (`.py` files): Do NOT need the executable bit. They are invoked through the `python-resolve.sh` interpreter resolver, e.g. `python-resolve.sh path/to/hook.py` (PI-361), so they run wherever Python is `python3`, `python`, or only available via `uv run`.

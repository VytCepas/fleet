#!/usr/bin/env python3
"""Adapt non-Claude agent hook payloads for the shared dag_workflow guard.

Codex (PreToolUse via .codex/hooks.json) and the GUI surfaces (cursor,
antigravity) reuse the same COMMAND_RULES enforcement Claude Code gets from
github_command_guard.sh. Payloads differ slightly per surface, so this shim
extracts the shell command, runs it through the Claude-side guards —
dag_workflow.py guard (lifecycle COMMAND_RULES), prod_guard.py
(destructive-command deny-list, autonomous mode; PI-394), and admission_package.py
(supply-chain package-existence check, autonomous mode; PI-564) — and
translates a deny to the caller's dialect:

  codex  -> {"hookSpecificOutput": {permissionDecision: "deny", ...}}
            (the documented PreToolUse schema; Codex accepts it)
  cursor -> {"permission": "deny", "user_message": ...}  (beforeShellExecution)
  antigravity -> {"decision": "deny", "reason": ...}      (safety-gate PreToolUse)

Stdin differs per surface too (PI-385, confirmed from vendor docs): Codex/Claude
send {"tool_input": {"command"}}, Cursor beforeShellExecution sends a top-level
{"command"}, Antigravity sends {"toolCall": {"args": {"CommandLine"}}}.

Fail-open by design: on any parse error the command proceeds — git hooks and CI
remain the real enforcement boundary (ADR-007). The GUI surfaces keep this posture
(no Cursor `failClosed`): a hook crash must never wedge a user's shell.

Usage (wired by the scaffolded hook configs): admission_agent_adapter.py <dialect>
"""

import json
import subprocess
import sys
from pathlib import Path


def _extract_command(payload: dict) -> str:
    """Pull the shell command out of whichever stdin shape the surface sent.

    Type-guards every level: malformed JSON (non-dict payload/tool_input/args)
    yields "" rather than raising, keeping the hook fail-open.
    """
    if not isinstance(payload, dict):
        return ""
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    tool_call = payload.get("toolCall")
    call_args = tool_call.get("args") if isinstance(tool_call, dict) else None
    if not isinstance(call_args, dict):
        call_args = {}
    command = (
        tool_input.get("command")
        or tool_input.get("cmd")
        or payload.get("command")  # Cursor beforeShellExecution (top-level)
        or call_args.get("CommandLine")  # Antigravity shell tool
        or call_args.get("command")
        or ""
    )
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    return str(command)


def _run_guard(name: str, payload: dict, *args: str) -> object:
    """Run a sibling guard script with a JSON payload; return its parsed verdict.

    Fail-open: a missing script or any error yields None (the command proceeds).
    """
    script = Path(__file__).with_name(name)
    if not script.exists():
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - executes only scaffold-owned sibling guard scripts.
            [sys.executable, str(script), *args],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
        )
        return json.loads(proc.stdout.strip() or "null")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def _deny_reason(verdict: object) -> str | None:
    """Deny reason from a PreToolUse verdict (documented hookSpecificOutput or the
    legacy {"decision": "block"} shape), or None if it isn't a deny."""
    if not isinstance(verdict, dict):
        return None
    hso = verdict.get("hookSpecificOutput")
    if isinstance(hso, dict) and hso.get("permissionDecision") == "deny":
        return hso.get("permissionDecisionReason", "")
    if verdict.get("decision") == "block":
        return verdict.get("reason", "")
    return None


def main() -> int:
    dialect = sys.argv[1] if len(sys.argv) > 1 else "codex"
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    command = _extract_command(payload)
    if not command:
        return 0
    cwd = payload.get("cwd") if isinstance(payload, dict) else None

    # Lifecycle COMMAND_RULES guard first; only run the next guard if the
    # previous one didn't already deny — avoids extra subprocesses on a
    # blocked command (PI-394/PI-564). Agent surfaces are non-interactive, so
    # both prod_guard and package_guard run in autonomous mode → a flagged
    # command blocks outright rather than "ask"ing. All three emit the
    # documented PreToolUse shape.
    reason = _deny_reason(
        _run_guard("dag_workflow.py", {"tool_input": {"command": command}}, "guard")
    )
    if reason is None:
        reason = _deny_reason(
            _run_guard(
                "prod_guard.py",
                {
                    "tool_input": {"command": command},
                    "permission_mode": "bypassPermissions",
                    "cwd": cwd or ".",
                },
            )
        )
    if reason is None:
        reason = _deny_reason(
            _run_guard(
                "admission_package.py",
                {
                    "tool_input": {"command": command},
                    "permission_mode": "bypassPermissions",
                },
            )
        )
    if reason is None:
        return 0

    if dialect == "cursor":
        # Cursor beforeShellExecution deny (PI-385: permission + snake_case msgs).
        out = {"permission": "deny", "user_message": reason, "agent_message": reason}
    elif dialect == "antigravity":
        # Antigravity safety-gate deny (PI-385).
        out = {"decision": "deny", "reason": reason}
    else:
        # codex: the documented PreToolUse schema (Codex accepts it).
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] in (["-h"], ["--help"]):  # --help does no work (#992)
        print((__doc__ or "").strip())
        sys.exit(0)
    sys.exit(main())

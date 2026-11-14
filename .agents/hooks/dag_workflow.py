#!/usr/bin/env python3
"""DAG-based workflow enforcement for the GitHub lifecycle.

Subcommands:
  check <node>          exit 0 if every prerequisite of <node> is satisfied,
                        exit 2 otherwise (with reason on stdout).
  guard                 read PreToolUse hook input JSON from stdin, map the
                        Bash command to a target node, emit a PreToolUse
                        hookSpecificOutput with permissionDecision: deny if
                        disallowed (the documented Claude Code schema).
  nodes                 list every DAG node and its prerequisites.
  push [<branch>] [N]   push current (or named) branch with retry + remote-SHA
                        verification (handles transient GitHub 5xx).
  promote [<pr>]        mark current (or numbered) draft PR ready for review.
  finish [<pr>] [--review-cycle N]
                        push, promote, then exec watch-pr.sh --merge.
  create-pr-nojira <type> <title> [--branch B] [--base B]
                        create a no-issue feature branch + draft PR.

The `check` and `guard` paths are pure read-only; they're used by hooks and
lifecycle scripts. The other subcommands consolidate the bash lifecycle
scripts (push-branch.sh, promote-review.sh, finish-pr.sh,
create-pr-without-issue.sh) so the tool is the single source of truth for the
GitHub workflow. The .sh files become thin shims that exec into here.

Issue-ref prefix detection:
  By default, branches matching `[A-Z]{2,}-<n>` (e.g. PI-98, ACME-42) are
  treated as issue-backed. Override with the DAG_ISSUE_PREFIX env var to pin
  a specific prefix (e.g. DAG_ISSUE_PREFIX=PROJ matches only `PROJ-<n>`).
  Branches with no recognized prefix fall through to the no-jira flow.

stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

GRAPH: dict[str, list[str]] = {
    "issue.created": [],
    "branch.created": [],
    "branch.pushed": ["branch.created"],
    "pr.opened": ["branch.pushed", "issue.created"],
    "ci.green": ["pr.opened"],
    "review.approved": ["pr.opened"],
    "pr.merged": ["ci.green", "review.approved"],
}

_CONFIGURED_PREFIX = os.environ.get("DAG_ISSUE_PREFIX", "").strip()
if _CONFIGURED_PREFIX:
    ISSUE_RE: re.Pattern[str] = re.compile(
        rf"\b{re.escape(_CONFIGURED_PREFIX)}-(\d+)\b", re.IGNORECASE
    )
else:
    ISSUE_RE = re.compile(r"\b[A-Z]{2,}-(\d+)\b")


def _run(cmd: list[str]) -> tuple[int, str]:
    # Per-call timeout must stay well below the hook budget (settings.json
    # "timeout": 60): a PreToolUse hook that exceeds its budget is killed and
    # FAILS OPEN, so one hung gh call must never eat the whole allowance.
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)  # noqa: S603 — argv list built from internal literals/validated state, never a shell string
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 1, ""
    return proc.returncode, proc.stdout


def _gh(args: list[str]) -> tuple[int, str]:
    return _run(["gh", *args])


def _git(args: list[str]) -> tuple[int, str]:
    return _run(["git", *args])


def _current_branch() -> str | None:
    code, out = _git(["branch", "--show-current"])
    branch = out.strip()
    return branch if code == 0 and branch else None


def _issue_from_branch(branch: str) -> int | None:
    m = ISSUE_RE.search(branch)
    return int(m.group(1)) if m else None


def check_issue_created() -> tuple[bool, str]:
    branch = _current_branch()
    if not branch:
        return False, "no current branch"
    n = _issue_from_branch(branch)
    if n is None:
        return True, f"branch '{branch}' has no issue ref (no-jira flow allowed)"
    code, out = _gh(["issue", "view", str(n), "--json", "number,state"])
    if code != 0:
        return False, f"issue #{n} not found via gh"
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        return False, f"issue #{n}: malformed gh output"
    if data.get("number") != n:
        return False, f"issue #{n} not present"
    return True, f"issue #{n} exists ({data.get('state', 'UNKNOWN')})"


def check_branch_created() -> tuple[bool, str]:
    branch = _current_branch()
    if not branch:
        return False, "not in a git repo / no current branch"
    if branch in {"main", "master"}:
        return False, "must be on a feature branch, not main/master"
    return True, f"on branch '{branch}'"


def check_branch_pushed() -> tuple[bool, str]:
    branch = _current_branch()
    if not branch:
        return False, "no current branch"
    code, _ = _git(["rev-parse", "--verify", f"origin/{branch}"])
    if code != 0:
        return False, f"origin/{branch} does not exist (push the branch first)"
    code, out = _git(["rev-list", "--count", f"origin/{branch}..HEAD"])
    if code == 0 and out.strip() and out.strip() != "0":
        return False, f"branch has {out.strip()} unpushed commit(s)"
    return True, f"branch '{branch}' is pushed and up to date with origin"


def check_pr_opened() -> tuple[bool, str]:
    code, out = _gh(["pr", "view", "--json", "number,state"])
    if code != 0:
        return False, "no PR exists for the current branch"
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        return False, "malformed gh pr view output"
    if data.get("state") != "OPEN":
        return False, f"PR is {data.get('state', 'unknown')}, not OPEN"
    return True, f"PR #{data.get('number')} is open"


def check_ci_green() -> tuple[bool, str]:
    code, out = _gh(["pr", "view", "--json", "number,statusCheckRollup"])
    if code != 0:
        return False, "cannot read PR / CI status"
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        return False, "malformed gh statusCheckRollup output"
    n = data.get("number", "?")
    rollup = data.get("statusCheckRollup") or []
    pending = failing = 0
    for entry in rollup:
        name = (entry.get("name") or entry.get("context") or "").lower()
        if "review/decision" in name:
            continue
        status = (entry.get("status") or "").upper()
        conclusion = (entry.get("conclusion") or "").upper()
        # StatusContext entries (classic commit statuses: Vercel/Netlify deploy
        # previews, Codecov, legacy CI integrations) carry `state` and have no
        # `status`/`conclusion`. Fold `state` in so a green commit status isn't
        # miscounted as "still running" forever — and a red one as pending —
        # while the CheckRun `conclusion`/`status` fields stay authoritative.
        state = (entry.get("state") or "").upper()
        if conclusion in {
            "FAILURE",
            "TIMED_OUT",
            "CANCELLED",
            "ERROR",
            "ACTION_REQUIRED",
        } or state in {
            "FAILURE",
            "ERROR",
        }:
            failing += 1
        elif (
            status in {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING"}
            or state in {"PENDING", "EXPECTED"}
            or (not conclusion and not status and not state)
        ):
            pending += 1
    if failing:
        return False, f"PR #{n}: {failing} CI check(s) failing"
    if pending:
        return False, f"PR #{n}: {pending} CI check(s) still running"
    return True, f"PR #{n}: CI green"


def check_review_approved() -> tuple[bool, str]:
    code, out = _gh(["pr", "view", "--json", "number,reviewDecision"])
    if code != 0:
        return False, "cannot read PR review decision"
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        return False, "malformed gh reviewDecision output"
    n = data.get("number", "?")
    decision = data.get("reviewDecision") or ""
    if decision == "APPROVED":
        return True, f"PR #{n}: review approved"
    if decision == "CHANGES_REQUESTED":
        return False, f"PR #{n}: review requested changes"
    return False, f"PR #{n}: review pending (decision={decision or 'none'})"


def check_pr_merged() -> tuple[bool, str]:
    code, out = _gh(["pr", "view", "--json", "number,state"])
    if code != 0:
        return False, "no PR for current branch"
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        return False, "malformed gh output"
    if data.get("state") == "MERGED":
        return True, f"PR #{data.get('number')} is merged"
    return False, f"PR #{data.get('number')} state is {data.get('state')}"


CHECKS = {
    "issue.created": check_issue_created,
    "branch.created": check_branch_created,
    "branch.pushed": check_branch_pushed,
    "pr.opened": check_pr_opened,
    "ci.green": check_ci_green,
    "review.approved": check_review_approved,
    "pr.merged": check_pr_merged,
}


def prereqs_satisfied(node: str, _seen: set[str] | None = None) -> tuple[bool, str]:
    """Walk all ancestors of `node` and return (True, '') if every prereq passes,
    else (False, '<first failing prereq>: <reason>').
    """
    if node not in GRAPH:
        return False, f"unknown node: {node}"
    seen = _seen if _seen is not None else set()
    if node in seen:
        return True, ""
    seen.add(node)
    for prereq in GRAPH[node]:
        ok, reason = CHECKS[prereq]()
        if not ok:
            return False, f"{prereq}: {reason}"
        ok, reason = prereqs_satisfied(prereq, seen)
        if not ok:
            return False, reason
    return True, "all prerequisites satisfied"


# Steering rules: command pattern -> (target_node | None, redirect_message)
# The first matching rule wins. target_node=None means a hard block with no
# DAG validation; otherwise, prereqs of target_node are appended to the reason.
# PI-845: create-issue.sh only files issues in THIS project, so the rule below
# must not dead-end the report-upstream-issue skill — `gh issue create` with an
# explicit -R/--repo naming a DIFFERENT repo has no wrapper equivalent and is
# allowed through (guard() checks _is_upstream_issue_create first).
_ISSUE_CREATE_RE = re.compile(r"\bgh\s+issue\s+create\b")
_ISSUE_REPO_FLAG_RE = re.compile(
    r"\bgh\s+issue\s+create\b[^;&|]*?(?:-R|--repo)[= ]+['\"]?([\w.-]+(?:/[\w.-]+)+)"
)
# origin URL → (host, owner/repo): strip scheme, then any `user@`, leaving
# `host[:port][:/]path`. Covers https, ssh://, and scp-like `git@host:owner/repo`.
_ORIGIN_URL_RE = re.compile(r"^(?:\w+://)?(?:[^@/]+@)?([\w.-]+)(?::\d+)?[:/](.+)$")


def _origin_host_slug() -> tuple[str, str] | None:
    """This repo's (host, owner/repo) from the origin remote (no network)."""
    try:
        url = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],  # noqa: S607 — read-only config probe, fixed argv
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 — a guard probe must never break the hook
        return None
    m = _ORIGIN_URL_RE.match(url)
    if not m:
        return None
    parts = m.group(2).lower().removesuffix(".git").strip("/").split("/")
    if len(parts) < 2:
        return None
    return m.group(1).lower(), "/".join(parts[-2:])


def _is_upstream_issue_create(cmd: str) -> bool:
    """True when the issue-create -R/--repo flag names a repo other than this one."""
    m = _ISSUE_REPO_FLAG_RE.search(cmd)
    if not m:
        return False
    # `gh --repo` accepts an optional host ([HOST/]OWNER/REPO). Compare the
    # owner/repo, and — when the flag carries an explicit host — the host too:
    # otherwise `-R github.com/OWNER/this-repo` parses as `github.com/OWNER` and
    # slips past the wrapper (PI-882), while a genuinely cross-host filing to a
    # same-named repo (e.g. a GHES project reporting to github.com/OWNER/REPO)
    # must still be allowed through (PI-882 review).
    parts = m.group(1).lower().removesuffix(".git").split("/")
    flag_host = parts[-3] if len(parts) >= 3 else None
    flag_slug = "/".join(parts[-2:])
    origin = _origin_host_slug()
    # Unknown origin: an explicit -R is still not this project's issue tracker
    # (create-issue.sh needs origin to work at all) — allow rather than dead-end.
    if origin is None:
        return True
    origin_host, origin_slug = origin
    if flag_slug != origin_slug:
        return True
    # Same owner/repo: an upstream filing only when an explicit, different host
    # is named; a bare or matching host is this project's own tracker.
    return flag_host is not None and flag_host != origin_host


COMMAND_RULES: list[tuple[re.Pattern[str], str | None, str]] = [
    (
        re.compile(r"git\s+push\b[^|;&\n]*?[\s:]['\"]?(?:refs/heads/)?(?:main|master)(?![\w./-])"),
        None,
        "Direct pushes to main/master are blocked. Open a feature branch and PR.",
    ),
    (
        # `[^&;]*?` allows flags between `gh api` and the endpoint (a real merge
        # needs `--method PUT`/`-X PUT`, often written before the path). `|` is NOT
        # excluded: a pipe inside a quoted flag (e.g. `--jq '.a|.b'`) must not let a
        # merge slip past the guard — only `;`/`&` end the command segment (PI-198).
        re.compile(r"\bgh\s+api\b[^&;]*?repos/[^/\s]+/[^/\s]+/pulls/\d+/merge\b"),
        "pr.merged",
        "Use .agents/scripts/watch-pr.sh <pr> --merge instead of `gh api .../merge` so CI and review gates are honored.",
    ),
    (
        # GraphQL merges bypass the REST-path rule above (2026-07 review): a
        # `gh api graphql` mutation can merge or enable auto-merge without ever
        # naming `/pulls/N/merge`. Block the mutation fields themselves.
        re.compile(
            r"\bgh\s+api\b[^&;]*\bgraphql\b[^&;]*"
            r"(?:mergePullRequest|enablePullRequestAutoMerge)"
        ),
        "pr.merged",
        "Use .agents/scripts/watch-pr.sh <pr> --merge instead of a `gh api graphql` merge mutation so CI and review gates are honored.",
    ),
    (
        re.compile(r"\bgh\s+pr\s+merge\b"),
        "pr.merged",
        "Use .agents/scripts/watch-pr.sh <pr> --merge instead of `gh pr merge` so CI, review waits, and review cycles are handled.",
    ),
    (
        re.compile(r"\bgh\s+pr\s+checks\b.*--watch"),
        None,
        "Use .agents/scripts/watch-pr.sh <pr> --merge instead of `gh pr checks --watch`.",
    ),
    (
        re.compile(r"\bgh\s+pr\s+ready\b"),
        "pr.opened",
        "Use .agents/scripts/promote-review.sh instead of `gh pr ready`.",
    ),
    (
        re.compile(r"\bgh\s+pr\s+create\b"),
        "pr.opened",
        "Use .agents/scripts/start-issue.sh (issue-backed) or .agents/scripts/create-pr-without-issue.sh (no issue) instead of `gh pr create`.",
    ),
    (
        _ISSUE_CREATE_RE,
        None,
        "Use .agents/scripts/create-issue.sh (or the start-task skill) so priority, references, and acceptance criteria are captured. Filing in ANOTHER repo (report-upstream-issue) is allowed with an explicit -R/--repo naming that repo.",
    ),
    (
        re.compile(r"\bgit\s+push\b"),
        "branch.pushed",
        "Use .agents/scripts/push-branch.sh instead of raw `git push` so transient GitHub failures are retried and the remote SHA is verified.",
    ),
]


_HEREDOC_RE = re.compile(
    r"<<-?\s*['\"]?(\w+)['\"]?[ \t]*\n.*?\n\1[ \t]*(?:\n|$)",
    re.DOTALL,
)

# Git global options that sit BETWEEN `git` and the subcommand (`git -C dir
# push`, `git -c k=v push`). Left unnormalized they defeat every `git push`
# rule, including the direct-push-to-main block (2026-07 review). Options that
# consume a value are listed with their value; boolean options stand alone.
# An option value is one shell word INCLUDING quoted runs — `-c foo='a b'`
# must normalize away too: a bare \S+ stops at the space inside the quotes
# and the mangled residue defeats every git rule (mirrors the quoted-value
# matcher in pre-commit-gate.sh).
_GIT_OPT_VAL = r"(?:[^\s'\"]|'[^']*'|\"[^\"]*\")+"
_GIT_GLOBAL_OPT = (
    r"(?:"
    rf"-C[ \t]+{_GIT_OPT_VAL}"  # -C <path>
    rf"|-c[ \t]+{_GIT_OPT_VAL}"  # -c <name=value>
    rf"|--git-dir(?:={_GIT_OPT_VAL}|[ \t]+{_GIT_OPT_VAL})"
    rf"|--work-tree(?:={_GIT_OPT_VAL}|[ \t]+{_GIT_OPT_VAL})"
    rf"|--namespace(?:={_GIT_OPT_VAL}|[ \t]+{_GIT_OPT_VAL})"
    rf"|--config-env(?:={_GIT_OPT_VAL}|[ \t]+{_GIT_OPT_VAL})"
    rf"|--exec-path(?:={_GIT_OPT_VAL})?"
    r"|--no-pager|--paginate|-p|-P|--bare"
    r"|--no-replace-objects|--literal-pathspecs|--no-optional-locks|--icase-pathspecs"
    r")"
)
_GIT_NORMALIZE_RE = re.compile(r"\bgit((?:[ \t]+" + _GIT_GLOBAL_OPT + r")+)[ \t]+")

# An interpreter receiving a heredoc executes its body, so a guarded command
# hidden in `bash <<'EOF' … EOF` must still be scanned (2026-07 review). The
# negative lookbehind lets `/bin/bash` match while sparing `rebash`.
_INTERP = r"(?:bash|sh|zsh|dash|ksh|python3?|python2|eval|source)"
_INTERP_INTRO_RE = re.compile(r"(?<![\w.-])" + _INTERP + r"\b")
_PIPE_INTERP_RE = re.compile(r"^\s*\|\s*(?:sudo[ \t]+)?(?<![\w.-])" + _INTERP + r"\b")


def _normalize_git_globals(cmd: str) -> str:
    """Rewrite ``git <global-opts> <subcommand>`` to ``git <subcommand>`` for
    matching, so interposed ``-C``/``-c``/``--git-dir`` flags can't smuggle a
    push past the rules (2026-07 review)."""
    return _GIT_NORMALIZE_RE.sub("git ", cmd)


def _strip_heredocs(cmd: str) -> str:
    """Remove heredoc body text so pattern rules don't fire on body content."""
    return _HEREDOC_RE.sub("", cmd)


def _executing_heredoc_bodies(cmd: str) -> str:
    """Concatenate heredoc bodies that WILL run — those fed to an interpreter
    directly (``bash <<EOF``) or via a pipe (``cat <<EOF … | bash``).

    ``_strip_heredocs`` removes every body to spare prose (a commit message or
    ``gh pr comment --body-file - <<EOF`` mentioning "git push main"), but that
    also hid genuinely-executing bodies. Re-surfacing only the interpreter-fed
    ones keeps prose exempt while closing the execution bypass (2026-07 review).
    """
    bodies = []
    for m in _HEREDOC_RE.finditer(cmd):
        line_start = cmd.rfind("\n", 0, m.start()) + 1
        intro = cmd[line_start : m.start()]
        after = cmd[m.end() : m.end() + 60]
        if _INTERP_INTRO_RE.search(intro) or _PIPE_INTERP_RE.match(after):
            bodies.append(m.group(0))
    return "\n".join(bodies)


# Free-text flag values carry arbitrary prose (commit messages, issue/PR comment
# and release bodies, titles) that must not be scanned for command patterns — a
# `--body` mentioning the literal "git push main" is data, not an invocation.
_TEXT_FLAG_RE = re.compile(
    r"""(?P<flag>--body|--message|--title|--notes|-b|-m|-t)   # known free-text flags
        (?:=|\s+)                                             # = or whitespace separator
        (?P<q>['"])                                           # opening quote
        (?P<val>(?:\\.|(?!(?P=q)).)*)                         # value (escapes allowed)
        (?P=q)                                                # matching closing quote
    """,
    re.VERBOSE | re.DOTALL,
)


def _strip_text_flag_values(cmd: str) -> str:
    """Blank the quoted value of known free-text flags so prose can't trip the
    pattern rules. Values containing a command substitution (``$(`` or a
    backtick) are left intact — those ARE executed, so the rules must still see
    them. Flag values are otherwise inert data: masking loses no real
    enforcement (an actual ``git push origin main`` uses a positional ref, not a
    flag value, and stays blocked)."""

    def _blank(m: re.Match[str]) -> str:
        val = m.group("val")
        if "$(" in val or "`" in val:
            return m.group(0)
        return f'{m.group("flag")} ""'

    return _TEXT_FLAG_RE.sub(_blank, cmd)


def _redirect_target_exists(reason: str) -> bool:
    """Best-effort: scan the redirect message for a `.agents/scripts/<name>`
    reference and check whether the file exists. If no reference is found,
    treat the rule as always-applicable (e.g. main/master block).
    """
    m = re.search(r"\.agents/scripts/([\w.-]+)", reason)
    if not m:
        return True
    # Resolve the PROJECT's wrapper-scripts dir, never the process CWD (#429).
    # Prefer $CLAUDE_PROJECT_DIR — Claude Code sets it on every hook invocation,
    # including the default plugin mode where this file lives under the plugin
    # root, not the project (#447 review). Otherwise — the codex/cursor/
    # antigravity adapter path, which runs the project's own .agents/hooks/ copy
    # and sets no such var — anchor on this file's location (.agents/hooks ->
    # .agents/scripts). Mirrors prod_guard.py's project-root resolution.
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        scripts_dir = Path(project_dir) / ".agents" / "scripts"
    else:
        scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    return (scripts_dir / m.group(1)).exists()


def guard(payload: dict) -> dict | None:
    cmd = ((payload.get("tool_input") or {}).get("command") or "").strip()
    if not cmd:
        return None

    # Strip heredoc bodies and free-text flag values so pattern rules don't fire
    # on prose content (e.g. `gh issue create --body "$(cat <<'EOF'\n...git
    # push...\nEOF\n)"`, or `gh pr comment --body "...git push main..."`), then
    # normalize interposed git global options so `git -C dir push` is matched
    # like `git push` (2026-07 review).
    cmd_scan = _normalize_git_globals(_strip_text_flag_values(_strip_heredocs(cmd)))
    # A heredoc fed to an interpreter DOES execute its body — scan those too, so
    # `bash <<'EOF' … git push … EOF` is not a bypass (2026-07 review).
    exec_bodies = _executing_heredoc_bodies(cmd)
    if exec_bodies:
        cmd_scan += "\n" + _normalize_git_globals(_strip_text_flag_values(exec_bodies))

    for pattern, target, message in COMMAND_RULES:
        if not pattern.search(cmd_scan):
            continue
        # PI-845: upstream issue filing has no wrapper — let it through.
        if pattern is _ISSUE_CREATE_RE and _is_upstream_issue_create(cmd_scan):
            continue
        # If the redirect points at a wrapper script that doesn't exist in
        # this repo, skip (don't block — there's nothing to redirect to).
        if not _redirect_target_exists(message):
            continue

        reason = message
        if target is not None:
            ok, why = prereqs_satisfied(target)
            if not ok:
                reason = f"{message}\n\nDAG prerequisite for {target} not met: {why}."
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    return None


def cmd_check(node: str) -> int:
    if node not in GRAPH:
        sys.stdout.write(f"unknown node: {node}\n")
        return 2
    ok, reason = prereqs_satisfied(node)
    if ok:
        # Also report the node's own check, for human consumption.
        own_ok, own_reason = CHECKS[node]()
        marker = "OK" if own_ok else "REACHABLE (state not yet satisfied)"
        sys.stdout.write(f"{marker}: {node} — {own_reason}\n")
        return 0  # prereqs satisfied = transition allowed (own check is advisory)
    sys.stdout.write(f"BLOCKED: cannot reach {node}: {reason}\n")
    return 2


def _redact_command(command: str) -> str:
    """Truncate to 500 chars and redact common secret patterns (mirrors prod_guard)."""
    cmd = command[:500]
    cmd = re.sub(r"(?i)(token|key|secret|password|auth|api_key)=[\w-]+", r"\1=***", cmd)
    return re.sub(r"://[^@]+@", r"://***@", cmd)


def _usage_log(payload: dict, decision: str) -> None:
    """Append one schema-pinned guard event to .agents/observability/usage.jsonl.

    Records the guard's ``decision`` (``allow``/``deny``) and the redacted
    ``command`` so a root orchestrator's governance signal isn't
    ``action=unknown`` (WS3). Dormant unless the observability overlay is
    installed (marker dir), and fully fail-open — logging must never break the
    guard. The event shape is pinned by schemas/usage-event.schema.json.
    """
    try:
        root_env = os.environ.get("CLAUDE_PROJECT_DIR")
        if root_env:
            root = Path(root_env)
        else:
            code, out = _git(["rev-parse", "--show-toplevel"])
            root = Path(out.strip()) if code == 0 and out.strip() else Path.cwd()
        obs = root / ".agents" / "observability"
        if not obs.is_dir():
            return
        command = ((payload.get("tool_input") or {}).get("command") or "").strip()
        line = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "hook": "github_command_guard",
            "event": "PreToolUse",
            "project": str(root),
            "decision": decision,
            "command": _redact_command(command),
        }
        session = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")
        if session:
            line["session"] = str(session)
        with (obs / "usage.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except Exception:  # noqa: BLE001 — logging must never break the guard
        return


def cmd_guard() -> int:
    raw = sys.stdin.read()
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    result = guard(payload)
    # "block"/"allow" matches the prod_guard/package_guard decision vocabulary.
    _usage_log(payload, "block" if result is not None else "allow")
    if result is not None:
        sys.stdout.write(json.dumps(result))
    return 0


def _detect_pr_number() -> int | None:
    code, out = _gh(["pr", "view", "--json", "number", "-q", ".number"])
    if code != 0:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def cmd_push(branch: str | None, max_retries: int, *, force: bool = False) -> int:
    """Push the current (or named) branch with retry + remote-SHA verification.

    Handles transient GitHub 5xx where `git push` exits non-zero but the
    commit actually landed on the remote. *force* adds --force-with-lease
    for the rebase-after-squash-merge case; main/master are refused.
    """
    if branch is None:
        branch = _current_branch()
    if not branch:
        sys.stderr.write("push: no current branch\n")
        return 1
    if branch in ("main", "master"):
        # Refuse main/master for ANY push, not only force-pushes — otherwise
        # running push-branch.sh while on main bypasses the direct-push guard,
        # since the internal `git push` subprocess is invisible to the
        # PreToolUse hook (PI-202).
        sys.stderr.write(
            "push: refusing to push main/master directly — open a feature branch + PR\n"
        )
        return 1

    code, sha_out = _git(["rev-parse", branch])
    if code != 0:
        sys.stderr.write(f"push: cannot resolve sha for {branch}\n")
        return 1
    expected_sha = sha_out.strip()

    def remote_has_sha() -> bool:
        code, out = _git(["ls-remote", "origin", f"refs/heads/{branch}"])
        if code != 0:
            return False
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0] == expected_sha:
                return True
        return False

    for attempt in range(max_retries + 1):
        push_cmd = ["git", "push", "-u", "origin", branch]
        if force:
            push_cmd.append("--force-with-lease")
        proc = subprocess.run(push_cmd)  # noqa: S603 — fixed git argv + validated branch name, never a shell string
        if proc.returncode == 0:
            sys.stdout.write(f"push: pushed {branch} ({expected_sha})\n")
            return 0
        if remote_has_sha():
            sys.stdout.write(
                f"push: remote already has {expected_sha} on {branch} "
                "(transient error, treating as success)\n"
            )
            _git(["branch", f"--set-upstream-to=origin/{branch}", branch])
            return 0
        if attempt < max_retries:
            time.sleep(3)
    sys.stderr.write(f"push: failed after {max_retries} retries\n")
    return 1


def cmd_promote(pr_number: int | None) -> int:
    """Mark a draft PR ready for review."""
    if pr_number is None:
        pr_number = _detect_pr_number()
    if pr_number is None:
        sys.stderr.write("promote: no PR found for current branch. Pass a PR number.\n")
        return 1
    code, out = _gh(["pr", "view", str(pr_number), "--json", "isDraft", "-q", ".isDraft"])
    if code != 0:
        sys.stderr.write(f"promote: cannot read PR #{pr_number}\n")
        return 1
    if out.strip() == "false":
        _, url = _gh(["pr", "view", str(pr_number), "--json", "url", "-q", ".url"])
        sys.stdout.write(f"PR #{pr_number} is already ready for review: {url.strip()}\n")
        return 0
    code, _ = _gh(["pr", "ready", str(pr_number)])
    if code != 0:
        sys.stderr.write(f"promote: gh pr ready failed for #{pr_number}\n")
        return code
    _, url = _gh(["pr", "view", str(pr_number), "--json", "url", "-q", ".url"])
    sys.stdout.write(f"PR #{pr_number} is now ready for review: {url.strip()}\n")
    return 0


def cmd_finish(pr_number: int | None, review_cycle: int | None) -> int:
    """Push, promote, then hand off to watch-pr.sh for CI/review/merge."""
    # Resolve the PR BEFORE pushing so we push that PR's head branch, never
    # whatever happens to be checked out. Pushing the current branch first let
    # a concurrent branch switch make `finish` push an unrelated branch (PI-458).
    if pr_number is None:
        pr_number = _detect_pr_number()
    if pr_number is None:
        sys.stderr.write("finish: no PR found for current branch.\n")
        return 1
    code, head = _gh(["pr", "view", str(pr_number), "--json", "headRefName", "-q", ".headRefName"])
    head = head.strip()
    if code != 0 or not head:
        sys.stderr.write(
            f"finish: cannot resolve head branch for PR #{pr_number} — refusing to push.\n"
        )
        return 1
    current = _current_branch()
    if current is None:
        sys.stderr.write(
            f"finish: no current branch (detached HEAD or not a git repo) — check out "
            f"PR #{pr_number}'s head '{head}' and re-run.\n"
        )
        return 1
    if current != head:
        sys.stderr.write(
            f"finish: checked-out branch '{current}' is not PR #{pr_number}'s head "
            f"'{head}'. Check out '{head}' and re-run — refusing to push the wrong branch.\n"
        )
        return 1
    rc = cmd_push(head, 3)
    if rc != 0:
        return rc
    rc = cmd_promote(pr_number)
    if rc != 0:
        return rc
    # Invoke via an explicit `bash` on an absolute path instead of exec'ing the
    # .sh directly (PI-361): that avoids depending on the script's executable
    # bit or shebang resolution (fragile on native Windows / Git Bash) and on
    # the current working directory. cmd_finish always runs from the scaffolded
    # .agents/hooks/ copy, so ../scripts/watch-pr.sh resolves in every mode.
    script = Path(__file__).resolve().parent.parent / "scripts" / "watch-pr.sh"
    monitor_args = ["bash", str(script), str(pr_number), "--merge"]
    if review_cycle is not None:
        monitor_args += ["--review-cycle", str(review_cycle)]
    return subprocess.run(monitor_args).returncode  # noqa: S603 — fixed bash argv + resolved script path, never a shell string


_VALID_TYPES = {"feat", "fix", "chore", "docs", "test"}
_BRANCH_RE = re.compile(r"^(feat|fix|chore|docs|test)/[A-Za-z0-9._/-]+$")


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return s.strip("-")


def cmd_create_pr_nojira(type_: str, title: str, branch: str | None, base: str | None) -> int:
    """Create a no-issue feature branch (if needed) and open a draft PR."""
    if type_ not in _VALID_TYPES:
        sys.stderr.write(
            f"ERROR: invalid type '{type_}'. Valid: {' '.join(sorted(_VALID_TYPES))}\n"
        )
        return 1
    if not title.strip():
        sys.stderr.write("ERROR: title must not be empty\n")
        return 1

    current = _current_branch()
    if not branch:
        if current and current not in {"main", "master"}:
            branch = current
        else:
            slug = _slugify(title)
            if not slug:
                sys.stderr.write("ERROR: title must contain at least one letter or number\n")
                return 1
            prefix = "nojira-"
            max_slug = max(12, 80 - len(type_) - 1 - len(prefix))
            slug = slug[:max_slug].rstrip("-")
            branch = f"{type_}/{prefix}{slug}"

    if not _BRANCH_RE.match(branch):
        sys.stderr.write(f"ERROR: branch '{branch}' must start with feat|fix|chore|docs|test/\n")
        return 1

    if current == branch:
        sys.stdout.write(f"Already on branch {branch}\n")
    else:
        code, _ = _git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
        if code == 0:
            sys.stdout.write(f"Branch {branch} already exists - switching\n")
            _git(["checkout", branch])
        else:
            _git(["checkout", "-b", branch])

    rc = cmd_push(branch, 3)
    if rc != 0:
        return rc

    # `gh pr view` with no selector also resolves the most recent CLOSED/MERGED
    # PR for the branch — reusing a branch name after a merge must open a new
    # PR, not report the merged one as "already exists" (matches check_pr_opened).
    # Selector passed explicitly: with none, gh resolves the branch through its
    # local remote-tracking ref, which a --single-branch/--depth clone never
    # creates for anything but the default branch (see _create_pr in
    # start-issue.sh). Naming the branch keeps the CLOSED/MERGED semantics above.
    code, out = _gh(["pr", "view", branch, "--json", "url,state"])
    if code == 0:
        try:
            data = json.loads(out or "{}")
        except json.JSONDecodeError:
            data = {}
        if data.get("state") == "OPEN" and data.get("url"):
            sys.stdout.write(f"Draft PR already exists: {data['url']}\n")
            return 0

    # Conventional Commits, no scope = no linked issue (ADR-006)
    pr_title = f"{type_}: {title}"
    pr_body = "No linked issue (nojira)."
    # Single trunk: with no explicit --base, gh targets the repo default branch.
    # --head explicit for the same reason as the selector above: gh's inference
    # depends on a remote-tracking ref that a narrow fetch refspec never creates.
    args = ["pr", "create", "--draft", "--head", branch, "--title", pr_title, "--body", pr_body]
    if base:
        args += ["--base", base]
    code, out = _gh(args)
    if code != 0:
        sys.stderr.write("create-pr-nojira: gh pr create failed\n")
        return code
    sys.stdout.write(f"Draft PR: {out.strip()}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dag_workflow")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="check whether a node is reachable")
    p_check.add_argument("node", help="DAG node name (e.g. pr.merged)")

    sub.add_parser("guard", help="PreToolUse hook entrypoint (reads stdin)")
    sub.add_parser("nodes", help="list all DAG nodes")

    p_push = sub.add_parser("push", help="push current branch with retry + SHA verify")
    p_push.add_argument("branch", nargs="?", default=None)
    p_push.add_argument("max_retries", nargs="?", type=int, default=3)
    p_push.add_argument(
        "--force-with-lease",
        action="store_true",
        dest="force_with_lease",
        help="force-push safely after a rebase (refused on main/master)",
    )

    p_promote = sub.add_parser("promote", help="mark a draft PR ready for review")
    p_promote.add_argument("pr_number", nargs="?", type=int, default=None)

    p_finish = sub.add_parser("finish", help="push, promote, monitor_pr --merge")
    p_finish.add_argument("pr_number", nargs="?", type=int, default=None)
    p_finish.add_argument("--review-cycle", type=int, default=None)

    p_nojira = sub.add_parser(
        "create-pr-nojira",
        help="create a no-issue branch + draft PR",
    )
    p_nojira.add_argument("type", choices=sorted(_VALID_TYPES))
    p_nojira.add_argument("title")
    p_nojira.add_argument("--branch", default=None)
    p_nojira.add_argument("--base", default=None)

    args = parser.parse_args(argv)

    if args.cmd == "check":
        return cmd_check(args.node)
    if args.cmd == "guard":
        return cmd_guard()
    if args.cmd == "nodes":
        for node, prereqs in GRAPH.items():
            sys.stdout.write(f"{node}: requires={prereqs or '[]'}\n")
        return 0
    if args.cmd == "push":
        return cmd_push(args.branch, args.max_retries, force=args.force_with_lease)
    if args.cmd == "promote":
        return cmd_promote(args.pr_number)
    if args.cmd == "finish":
        return cmd_finish(args.pr_number, args.review_cycle)
    if args.cmd == "create-pr-nojira":
        return cmd_create_pr_nojira(args.type, args.title, args.branch, args.base)
    return 1


if __name__ == "__main__":
    sys.exit(main())

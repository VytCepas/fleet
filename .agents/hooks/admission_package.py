"""Package-existence guard (PI-564): supply-chain check before any install.

PreToolUse hook on Bash. Intercepts `uv add`, `bun add`, `pip install`,
`npm install`, and `cargo add`, and checks each package name against its
registry before letting the install through:

- a name that doesn't exist on the registry (404) is very likely a typo or a
  hallucinated dependency name — flagged before the install can typosquat
  or simply fail with a confusing error further down;
- a name that DOES exist but sits within a small edit distance of a
  well-known popular package (e.g. ``reqeusts`` vs ``requests``) is flagged
  as a possible typosquat.

Same ask/deny split as prod_guard.py (ADR-012):

- ``ask``  in interactive sessions — a human confirms or rejects;
- ``deny`` in fully autonomous sessions — there is no human to ask.

The popular-package lists below are a small curated subset per ecosystem,
not a literal top-1000 — good enough to catch the common typosquat targets
without embedding (and maintaining) a large static list.

Fail-open by design, in two senses: a network failure never blocks an
install (better to skip the check than to wedge the agent when offline),
and any internal error lets the command proceed, same as prod_guard.py.
This is a guardrail, not the security boundary (ADR-007) — lockfile pinning
and hash verification are what actually stop a compromised dependency.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# (pattern matched at the START of a command segment, ecosystem)
_INSTALL_VERBS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\s*uv\s+add\b"), "pypi"),
    (re.compile(r"^\s*bun\s+add\b"), "npm"),
    (re.compile(r"^\s*pip\s+install\b"), "pypi"),
    (re.compile(r"^\s*npm\s+install\b"), "npm"),
    (re.compile(r"^\s*cargo\s+add\b"), "crates"),
]

# Registry lookup base URLs — overridable via env for tests (a local mock
# HTTP server) without touching the real network.
_REGISTRY_URL_TEMPLATES = {
    "pypi": os.environ.get("PACKAGE_GUARD_PYPI_URL", "https://pypi.org/pypi/{name}/json"),
    "npm": os.environ.get("PACKAGE_GUARD_NPM_URL", "https://registry.npmjs.org/{name}"),
    "crates": os.environ.get("PACKAGE_GUARD_CRATES_URL", "https://crates.io/api/v1/crates/{name}"),
}

_TIMEOUT_SECONDS = 2.0

# Curated top packages per ecosystem — typosquat reference set, not exhaustive.
_POPULAR: dict[str, set[str]] = {
    "pypi": {
        "requests",
        "numpy",
        "pandas",
        "flask",
        "django",
        "pytest",
        "boto3",
        "urllib3",
        "click",
        "pyyaml",
        "setuptools",
        "pip",
        "wheel",
        "certifi",
        "idna",
        "charset-normalizer",
        "six",
        "python-dateutil",
        "attrs",
        "packaging",
        "cryptography",
        "jinja2",
        "markupsafe",
        "sqlalchemy",
        "pydantic",
        "aiohttp",
        "fastapi",
        "uvicorn",
        "scipy",
        "matplotlib",
        "pillow",
        "beautifulsoup4",
        "lxml",
        "pyjwt",
        "redis",
        "celery",
        "gunicorn",
        "httpx",
        "typer",
        "rich",
    },
    "npm": {
        "react",
        "react-dom",
        "lodash",
        "express",
        "axios",
        "vue",
        "webpack",
        "typescript",
        "eslint",
        "prettier",
        "jest",
        "babel",
        "chalk",
        "commander",
        "moment",
        "dayjs",
        "uuid",
        "async",
        "underscore",
        "jquery",
        "next",
        "vite",
        "rollup",
        "yargs",
        "dotenv",
        "cors",
        "body-parser",
        "mongoose",
        "socket.io",
        "prop-types",
        "classnames",
        "redux",
        "rxjs",
        "tslib",
        "semver",
        "glob",
        "minimist",
        "debug",
        "chokidar",
    },
    "crates": {
        "serde",
        "tokio",
        "clap",
        "rand",
        "regex",
        "log",
        "anyhow",
        "thiserror",
        "reqwest",
        "async-trait",
        "futures",
        "chrono",
        "uuid",
        "serde_json",
        "itertools",
        "once_cell",
        "lazy_static",
        "bytes",
        "hyper",
        "tracing",
        "clap_derive",
        "num_cpus",
        "crossbeam",
        "parking_lot",
        "rayon",
        "syn",
        "quote",
        "proc-macro2",
        "libc",
        "cfg-if",
        "bitflags",
    },
}

# Flags that consume the NEXT token as a value, not a package name (e.g.
# `uv add --group dev requests` — "dev" is a PEP 735 group name, not a
# package). Not exhaustive — a missed flag just means one extra registry
# lookup, which is harmless.
_VALUE_TAKING_FLAGS: dict[str, set[str]] = {
    "pypi": {
        "--group",
        "-g",
        "--extra",
        "--optional",
        "--index",
        "--index-url",
        "-i",
        "--extra-index-url",
        "--find-links",
        "-f",
        "--target",
        "--prefix",
        "--constraint",
        "-c",
        "--requirement",
        "-r",
        "--python",
        "--python-version",
        "--script",
        "--package",
    },
    "npm": {"--registry", "--tag", "--save-prefix"},
    "crates": {"--features", "-F", "--rename", "--target", "--registry"},
}

# Fully autonomous mode: no human is watching the prompt, so "ask" is
# meaningless — block outright. Other modes still surface an interactive
# permission prompt for Bash.
_AUTONOMOUS_MODES = {"bypassPermissions", "dangerouslySkipPermissions"}


def _find_obs_dir(start: Path) -> Path | None:
    """Locate the overlay marker dir (.agents/observability/), or None.

    Prefers ``$CLAUDE_PROJECT_DIR``; otherwise walks up from *start* (the Bash
    cwd, which may be a subdirectory after ``cd``), mirroring ``_find_config``.
    """
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        obs = Path(env) / ".agents" / "observability"
        return obs if obs.is_dir() else None
    for candidate in (start, *start.parents):
        obs = candidate / ".agents" / "observability"
        if obs.is_dir():
            return obs
    return None


def _redact_command(command: str) -> str:
    """Truncate to 500 chars and redact common secret patterns."""
    cmd = command[:500]
    cmd = re.sub(r"(?i)(token|key|secret|password|auth|api_key)=[\w-]+", r"\1=***", cmd)
    return re.sub(r"://[^@]+@", r"://***@", cmd)


def usage_log(payload: dict, root: Path, decision: str, command: str) -> None:
    """Append a self-log line iff the observability overlay is installed (#406).

    Shipped-always-dormant: no-ops unless ``.agents/observability/`` exists.
    Uses the *already-parsed* ``payload`` (no second stdin read) and is fully
    fail-open — it must never raise or block the guard.
    """
    try:
        obs = _find_obs_dir(root)
        if obs is None:
            return
        line = {
            # time.gmtime keeps this portable across every Python 3 (no
            # datetime.UTC, which is 3.11+) — scaffolded projects may run older.
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "hook": "package_guard",
            "event": "PreToolUse",
            "project": str(obs.parent.parent),
            "decision": decision,
            "command": _redact_command(command),
        }
        session = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")
        if session:
            line["session"] = session
        with (obs / "usage.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except Exception:  # noqa: BLE001 — logging must never break the guard
        return


def _extract_packages(remainder: str, ecosystem: str) -> list[str]:
    """Pull candidate package names out of the args after the install verb.

    Best-effort tokenizer, not a full CLI-argument parser, but flags that
    take a value (e.g. `--group dev`, `-F derive`) have that value skipped
    too — otherwise `uv add --group dev requests` would check "dev" against
    PyPI and false-positive-flag an extremely common, entirely legitimate
    command. `--flag=value` inline form carries its own value already, so
    only the separate `--flag value` form needs the lookahead skip.
    Local paths and VCS URLs are skipped outright (not registry packages).
    """
    try:
        tokens = shlex.split(remainder)
    except ValueError:
        return []
    value_flags = _VALUE_TAKING_FLAGS.get(ecosystem, set())
    packages: list[str] = []
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if not tok:
            continue
        if tok.startswith("-"):
            if "=" not in tok and tok in value_flags:
                skip_next = True
            continue
        if tok.startswith(("git+", "http://", "https://", "git@", ".", "/")):
            continue
        name = tok
        if ecosystem == "pypi":
            m = re.match(r"^([A-Za-z0-9_.-]+)", tok)
            name = m.group(1) if m else tok
        elif ecosystem == "npm":
            if name.startswith("@"):
                at_positions = [i for i, c in enumerate(name) if c == "@"]
                if len(at_positions) > 1:
                    name = name[: at_positions[1]]
            elif "@" in name:
                name = name.split("@", 1)[0]
        elif ecosystem == "crates":
            if "@" in name:
                name = name.split("@", 1)[0]
        if name:
            packages.append(name)
    return packages


def _registry_status(ecosystem: str, name: str) -> int | None:
    """HTTP status for *name* on its registry, or None on any network failure
    (fail-open — a check that can't complete must never block the install)."""
    template = _REGISTRY_URL_TEMPLATES.get(ecosystem)
    if template is None:
        return None
    url = template.format(name=urllib.parse.quote(name, safe=""))
    try:
        req = urllib.request.Request(  # noqa: S310 — url is built from a fixed https:// template
            url, headers={"User-Agent": "project-init-package-guard"}
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310 — same fixed https:// template
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001 — any network failure fails open
        return None


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1]


def check_package(ecosystem: str, name: str) -> str | None:
    """Return a flagged-reason string for *name*, or None if it's fine."""
    popular = _POPULAR.get(ecosystem, set())
    lname = name.lower()
    if lname in popular:
        return None
    status = _registry_status(ecosystem, name)
    if status is None:
        return None  # network unavailable — fail open
    if status == 404:
        return f"{name!r} was not found on {ecosystem} — check for a typo before installing"
    if len(lname) > 4:
        for candidate in popular:
            if lname != candidate and _edit_distance(lname, candidate) <= 2:
                return (
                    f"{name!r} exists on {ecosystem} but is suspiciously close to the "
                    f"popular package {candidate!r} — possible typosquat, verify before installing"
                )
    return None


def evaluate(command: str, permission_mode: str) -> dict | None:
    """Return the hook verdict for *command*, or None to let it through."""
    segments = re.split(r"&&|\|\||;|\|", command)
    flags: list[str] = []
    for segment in segments:
        for pattern, ecosystem in _INSTALL_VERBS:
            match = pattern.match(segment)
            if not match:
                continue
            for name in _extract_packages(segment[match.end() :], ecosystem):
                warning = check_package(ecosystem, name)
                if warning:
                    flags.append(warning)
    if not flags:
        return None
    reason = "package_guard: " + "; ".join(flags)
    decision = "deny" if permission_mode in _AUTONOMOUS_MODES else "ask"
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }


def main() -> int:
    """Read the PreToolUse payload from stdin; print a verdict if any."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    command = (tool_input.get("command") or "").strip()
    if not command:
        return 0
    mode = payload.get("permission_mode") or payload.get("permissionMode") or ""
    root = Path(payload.get("cwd") or ".")
    try:
        verdict = evaluate(command, mode)
    except Exception:  # noqa: BLE001 — guardrail must never break the session
        verdict = None

    decision = "allow"
    if verdict is not None:
        raw_decision = verdict.get("hookSpecificOutput", {}).get("permissionDecision", "allow")
        decision = "block" if raw_decision == "deny" else raw_decision

    # Self-log this firing from the same parsed payload (no second stdin read,
    # #406). Dormant unless the observability overlay is installed; fail-open.
    usage_log(payload, root, decision, command)

    if verdict is not None:
        sys.stdout.write(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] in (["-h"], ["--help"]):  # --help does no work (#992)
        print((__doc__ or "").strip())
        sys.exit(0)
    sys.exit(main())

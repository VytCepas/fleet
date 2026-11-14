#!/usr/bin/env python3
"""Mechanise the cross-repo test contract's rules that pytest itself cannot see.

    check-test-contract.py exit-codes [PATH ...]   (default: tests)
    check-test-contract.py discovery [DIR]         (default: tests)

exit-codes (rule 2, exact exit codes): a test asserts the exit code it expects,
never "non-zero". A crash, a usage error and a refusal are different answers,
and ``assert proc.returncode != 0`` passes all three. Flagged, in any
``assert``: ``!= 0`` or ``> 0`` against an exit code (``.returncode``,
``.exit_code``, ``rc`` ...), ``not ... == 0``, or the exit code's bare
truthiness. An ``if returncode != 0:`` guard is not an assertion and is left
alone. The ratchet is zero: an assertion that truly cannot name one code keeps
its place only with a reason on its first line::

    assert proc.returncode != 0  # test-contract: nonzero-ok: <why no one code>

discovery (rule 4, every suite is in the manifest): for pytest the manifest is
discovery itself, so a file under DIR that defines a ``test_`` function (at
module level, or in a ``Test*`` class) but that a full ``pytest --collect-only``
from the current directory does not reach runs nowhere, and nothing says so:
``tests/check_upgrade.py`` holding ``def test_...`` is the usual case. Rename
it to ``test_*.py``, or move the function out if it is not a test. Collection
runs under ``sys.executable``, so run this with the project's interpreter
(``uv run python``).

Exit codes: 0 clean, 1 a finding (each printed as path:line), 2 usage. Stdlib only.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

_EXIT_ATTRS = {"returncode", "exit_code", "exitcode", "exit_status"}
_EXIT_NAMES = re.compile(r"^(?:.*_)?(?:rc|returncode|exit_code|exitcode|exit_status)$")
_EXEMPT = re.compile(r"#\s*test-contract:\s*nonzero-ok:\s*\S")


def _is_exit_code(node: ast.expr) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr in _EXIT_ATTRS
    return isinstance(node, ast.Name) and bool(_EXIT_NAMES.match(node.id))


def _is_zero(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value == 0 and not isinstance(node.value, bool)


def _nonzero_compare(node: ast.Compare) -> bool:
    """`<code> != 0`, `<code> > 0`, `0 != <code>` or `0 < <code>`."""
    if len(node.ops) != 1:
        return False
    left, op, right = node.left, node.ops[0], node.comparators[0]
    if _is_exit_code(left) and _is_zero(right):
        return isinstance(op, (ast.NotEq, ast.Gt))
    if _is_zero(left) and _is_exit_code(right):
        return isinstance(op, (ast.NotEq, ast.Lt))
    return False


def _eq_zero(node: ast.expr) -> bool:
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return False
    sides = (node.left, node.comparators[0])
    return (
        isinstance(node.ops[0], ast.Eq)
        and any(map(_is_zero, sides))
        and any(map(_is_exit_code, sides))
    )


def _asserts_nonzero(test: ast.expr) -> bool:
    """True when *test*, or any `and`/`or` operand of it, only says "not zero".

    An `or` operand counts too: `assert rc != 0 or cond` passes for every non-zero code.
    """
    if isinstance(test, ast.BoolOp):
        return any(_asserts_nonzero(value) for value in test.values)
    if isinstance(test, ast.Compare):
        return _nonzero_compare(test)
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _eq_zero(test.operand)
    return _is_exit_code(test)


def exit_code_findings(path: Path) -> list[str]:
    """Each non-exact exit-code assertion in *path*, as ``path:line: text``."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as err:
        return [f"{path}:{err.lineno}: cannot parse ({err.msg})"]
    lines = source.splitlines()
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert) and _asserts_nonzero(node.test):
            text = lines[node.lineno - 1]
            if not _EXEMPT.search(text):
                findings.append(f"{path}:{node.lineno}: {text.strip()}")
    return findings


def _defines_tests(path: Path) -> int | None:
    """The line of the first test function pytest would look for in *path*, else None."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return None
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in tree.body:
        if isinstance(node, funcs) and node.name.startswith("test"):
            return node.lineno
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, funcs) and item.name.startswith("test"):
                    return item.lineno
    return None


def collected_files(root: Path) -> set[Path] | str:
    """The files a full pytest collection from *root* reaches, or why it failed."""
    argv = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [*argv, f"--rootdir={root}"], capture_output=True, text=True, cwd=root, check=False
    )
    if proc.returncode not in (0, 5):  # 5: nothing collected, which is an answer
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
        return f"pytest --collect-only exited {proc.returncode}:\n{tail}"
    return {
        (root / line.split("::", 1)[0]).resolve()
        for line in proc.stdout.splitlines()
        if "::" in line
    }


def discovery_findings(root: Path, tests: Path) -> list[str]:
    """Each file under *tests* that defines a test no full collection reaches."""
    candidates = {}
    for path in sorted(tests.rglob("*.py")):
        line = _defines_tests(path)
        if line is not None:
            candidates[path.resolve()] = (path, line)
    if not candidates:
        return []
    reached = collected_files(root)
    if isinstance(reached, str):
        return [reached]
    return [
        f"{path}:{line}: defines a test that pytest never collects"
        for resolved, (path, line) in candidates.items()
        if resolved not in reached
    ]


def _python_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        files.extend([root] if root.is_file() else sorted(root.rglob("*.py")))
    return files


def main(argv: list[str] | None = None) -> int:
    """Run one check; return its exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="check", required=True)
    codes = sub.add_parser("exit-codes", help="rule 2: no 'non-zero' exit-code assertion")
    codes.add_argument("paths", nargs="*", type=Path, default=[Path("tests")])
    found = sub.add_parser("discovery", help="rule 4: every test file is collected")
    found.add_argument("dir", nargs="?", type=Path, default=Path("tests"))
    args = parser.parse_args(argv)
    if args.check == "discovery":
        return _discovery(parser, args.dir)
    missing = [str(p) for p in args.paths if not p.exists()]
    if missing and missing != ["tests"]:
        parser.error(f"no such path: {', '.join(missing)}")
    findings = [
        f
        for p in _python_files([p for p in args.paths if p.exists()])
        for f in exit_code_findings(p)
    ]
    for finding in findings:
        print(finding)
    if findings:
        print(
            f"test contract rule 2: {len(findings)} assertion(s) accept any non-zero exit. "
            "Assert the exact code, or add `# test-contract: nonzero-ok: <reason>`.",
            file=sys.stderr,
        )
        return 1
    return 0


def _discovery(parser: argparse.ArgumentParser, tests: Path) -> int:
    if not tests.is_dir():
        if tests == Path("tests"):
            return 0  # nothing to discover yet
        parser.error(f"no such directory: {tests}")
    findings = discovery_findings(Path.cwd(), tests)
    for finding in findings:
        print(finding)
    if findings:
        print(
            f"test contract rule 4: {len(findings)} file(s) under {tests} run nowhere. "
            "Name them test_*.py or *_test.py, or move the non-test out.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

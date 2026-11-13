#!/usr/bin/env python3
"""Run a test runner and end with the test contract's line (rule 3; #1054).

    contract-line.py <suite> <bun|go|cargo> -- <runner argv ...>

The runner's output passes through (stderr folded into stdout), then the last
line is ``<suite>: N passed, M failed`` and the exit code is the runner's own,
so a fleet runner can add the numbers up. A skip counts in neither.

- bun: the closing ``N pass`` / ``N fail`` block before ``Ran N tests``; an
  ``N error`` line counts as failed. A test's own ``100 pass`` output is not counted.
- go: run it as ``go test -json``. Top-level tests are counted (a subtest's
  result already decides its parent's), and the output printed is go's plain
  form: package lines, plus the log of each test and subtest that failed.
- cargo: every harness ``test result:`` line (lib, each test binary, doc-tests)
  is summed; one inside a failing test's captured output is not.

A run that never reaches its tests (a build error) prints ``0 passed, 0 failed``
beside a non-zero exit, which the contract reads as a run that did not finish.
Stdlib only.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator

_BUN = re.compile(r"^\s*(\d+) (pass|fail|errors?)\s*$")
_BUN_EXTRA = re.compile(
    r"^\s*(?:\d+ (?:skip|todo|expect\(\) calls|snapshots?)|\d+ snapshots?,.*)\s*$"
)
_BUN_RAN = re.compile(r"^Ran \d+ tests? across \d+ files?\b")
_CARGO = re.compile(r"^test result: \w+\. (\d+) passed; (\d+) failed;")
_CARGO_CAPTURE = re.compile(r"^---- .* stdout ----$")


def bun(lines: Iterable[str]) -> Iterator[tuple[str, int, int]]:
    """Pass bun's output through; yield the totals of its closing summary block.

    The block (``N pass`` / ``N fail`` ... ) is the run of summary lines directly
    before ``Ran N tests across M files``; a test's own ``100 pass`` stdout is
    never followed by that line, so it is not counted.
    """
    passed = failed = 0
    for line in lines:
        m = _BUN.match(line)
        if m is not None:
            if m[2] == "pass":  # the block opens with `N pass`: anything before it is stray
                passed, failed = int(m[1]), 0
            else:
                failed += int(m[1])
        elif _BUN_EXTRA.match(line):
            pass
        elif _BUN_RAN.match(line):
            yield line, passed, failed
            passed = failed = 0
            continue
        else:
            passed = failed = 0
        yield line, 0, 0


def cargo(lines: Iterable[str]) -> Iterator[tuple[str, int, int]]:
    """Pass cargo's output through; yield each test binary's totals.

    Cargo reprints a failing test's stdout verbatim under ``---- name stdout ----``
    until the closing ``failures:`` list; a ``test result:`` inside is test output.
    """
    captured = False
    previous = ""
    for line in lines:
        if _CARGO_CAPTURE.match(line):
            captured = True
        elif captured and line.strip() == "failures:" and not previous.strip():
            captured = False
        m = None if captured else _CARGO.match(line)
        previous = line
        yield (line, int(m[1]), int(m[2])) if m else (line, 0, 0)


def go(lines: Iterable[str]) -> Iterator[tuple[str, int, int]]:
    """Render ``go test -json`` in go's plain form; count top-level test results."""
    held: dict[tuple[str, str], list[str]] = {}
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            yield line, 0, 0  # not an event: a build error, or go itself
            continue
        if not isinstance(event, dict):
            yield line, 0, 0
            continue
        test, action = event.get("Test", ""), event.get("Action", "")
        key = (event.get("Package", ""), test)
        if action == "output" and test:
            held.setdefault(key, []).append(event.get("Output", ""))
        elif action in ("output", "build-output"):
            yield event.get("Output", ""), 0, 0
        elif action in ("pass", "fail", "skip") and test:
            log = held.pop(key, [])
            top = "/" not in test
            if action == "fail":
                yield "".join(log), 0, int(top)  # a subtest's log says why; only the parent counts
            elif action == "pass" and top:
                yield "", 1, 0


_PARSERS = {"bun": bun, "go": go, "cargo": cargo}


def main(argv: list[str]) -> int:
    """Run the runner in *argv*; return its exit code."""
    if argv[:1] in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    if len(argv) < 4 or argv[1] not in _PARSERS or argv[2] != "--":
        sys.stderr.write(__doc__.split("\n\n")[1] + "\n")
        return 2
    suite, parser, runner = argv[0], _PARSERS[argv[1]], argv[3:]
    passed = failed = 0
    try:
        proc = subprocess.Popen(  # noqa: S603 — the recipe's own argv, no shell
            runner, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
    except OSError as err:
        print(f"contract_line: cannot run {runner[0]}: {err.strerror}", file=sys.stderr)
        print(f"{suite}: 0 passed, 0 failed")
        return 127
    for text, p, f in parser(proc.stdout or ()):
        passed, failed = passed + p, failed + f
        if text:
            sys.stdout.write(text if text.endswith("\n") else text + "\n")
            sys.stdout.flush()
    code = proc.wait()
    print(f"{suite}: {passed} passed, {failed} failed")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

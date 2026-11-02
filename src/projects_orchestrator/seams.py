"""Is every cross-repo copy still what its producer ships (box register, seams).

``freshness.py`` answers that for one seam, the descriptor contract vendored from
project-init, against the network. This generalises it: the ambient layer's ``contracts/seams.tsv``
is the list of seams, and each row's copy is compared with its producer's checkout.
A copy that no row names is a finding too, so a new vendored tree cannot appear
without a row.

Pure where it can be: :func:`check` takes the register, a map of system name to
checkout, and a :data:`Reader` that returns a file at a ref, so the unit tests build
throwaway git repos and never touch the network. Never raises on a missing checkout:
that row is ``unchecked``, which is not a pass.

The findings go to a ticket through the ambient layer's findings channel
(its ``bin/standard-seams-fresh.sh``), never to a red PR: a producer moving is not a
reason to block the consumer's work, it is a reason to re-vendor on purpose.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from projects_orchestrator.freshness import _contract

CHECK_ID = "check.seam-fresh"
_COLUMNS = (
    "id",
    "kind",
    "producer",
    "consumer",
    "copy_path",
    "pin",
    "schema",
    "freshness",
    "owner",
    "allowed_lag",
    "reason",
)

# A reader: (system, path, ref) -> the file's bytes at that ref, or None when it is not there.
Reader = Callable[[str, str, str], bytes | None]


@dataclass(frozen=True)
class Seam:
    """One row of ``contracts/seams.tsv``.

    Attributes:
        id: The row's key.
        kind: ``vendor`` | ``contract`` | ``format`` | ``upstream``.
        producer: The system that owns the contract.
        consumer: The system that holds the copy.
        copy_path: The consumer's path to the copy, ``-`` when it reads in place.
        pin: ``<path>#<key>`` locating the recorded origin, ``-`` when none.
        schema: The producer's path to its schema, ``-`` when none.
        freshness: The control that compares the copy, ``-`` when there is none.
        allowed_lag: Producer commits the pin may trail before staleness is a finding.
    """

    id: str
    kind: str
    producer: str
    consumer: str
    copy_path: str
    pin: str
    schema: str
    freshness: str
    allowed_lag: int


@dataclass(frozen=True)
class Finding:
    """One thing wrong with a seam.

    Attributes:
        seam: The row id, or ``copy:<path>`` for a copy with no row.
        kind: ``stale`` | ``missing-copy`` | ``no-schema`` | ``unregistered`` | ``unchecked``.
        detail: What differs, in one line.
    """

    seam: str
    kind: str
    detail: str

    def line(self) -> str:
        """The finding as one tab-separated line: seam, kind, detail."""
        return f"{self.seam}\t{self.kind}\t{self.detail}"


def load_register(text: str) -> list[Seam]:
    """Parse ``seams.tsv`` text into rows, skipping comments and the header (pure)."""
    rows: list[Seam] = []
    for raw in text.splitlines():
        cells = raw.split("\t")
        if not raw or raw.startswith("#") or cells[0] == "id" or len(cells) < len(_COLUMNS):
            continue
        lag = cells[9]
        rows.append(
            Seam(
                cells[0],
                cells[1],
                cells[2],
                cells[3],
                cells[4],
                cells[5],
                cells[6],
                cells[7],
                allowed_lag=int(lag) if lag.isdigit() else 0,
            )
        )
    return rows


def _sha(data: bytes) -> str:
    """SHA-256 hex of ``data`` (pure)."""
    return hashlib.sha256(data).hexdigest()


def _git(root: Path, *args: str) -> tuple[int, bytes]:
    """Run ``git -C root args`` and return (exit code, stdout); never raises."""
    try:
        done = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(root), *args],  # noqa: S607 — git from PATH, as elsewhere here
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        # expected: git missing or hung is the answer; the caller reports the seam unchecked
        return 1, b""
    return done.returncode, done.stdout


def git_reader(roots: dict[str, Path]) -> Reader:
    """A :data:`Reader` that reads ``ref:path`` from the system's checkout with ``git show``."""

    def read(system: str, path: str, ref: str) -> bytes | None:
        root = roots.get(system)
        if root is None:
            return None
        code, out = _git(root, "show", f"{ref}:{path}")
        return out if code == 0 else None

    return read


def default_ref(root: Path) -> str:
    """``origin/main`` when the checkout has it, else ``HEAD`` (the producer's published state)."""
    code, _ = _git(root, "rev-parse", "--verify", "--quiet", "origin/main")
    return "origin/main" if code == 0 else "HEAD"


def _manifest(text: str) -> tuple[str, list[tuple[str, str, str]]]:
    """The pin and the ``(path, sha256, source)`` rows of a vendored MANIFEST.tsv (pure)."""
    pin = ""
    rows: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        if re.match(r"^#\s*\w+_commit:", line):
            pin = line.split(":", 1)[1].strip()
        elif line and not line.startswith("#"):
            cells = line.split("\t")
            if len(cells) >= 3:
                rows.append((cells[0], cells[1], cells[2]))
    return pin, rows


def _vendor(seam: Seam, roots: dict[str, Path], ref: str, read: Reader) -> list[Finding]:
    """A vendored tree against its producer: files whose hash moved, and the pin's lag."""
    manifest = (roots[seam.consumer] / seam.copy_path / seam.pin.split("#")[0]).read_text("utf-8")
    pin, rows = _manifest(manifest)
    stale: list[str] = []
    for path, sha, source in rows:
        now = read(seam.producer, source, ref)
        if now is None:
            stale.append(f"{path} (gone from {seam.producer})")
        elif _sha(now) != sha:
            stale.append(path)
    if not stale:
        return []
    sources = [source for _, _, source in rows]
    code, out = _git(roots[seam.producer], "rev-list", "--count", f"{pin}..{ref}", "--", *sources)
    if code != 0:
        return [
            Finding(
                seam.id, "unchecked", f"the pin {pin[:7]} is not in the {seam.producer} checkout"
            )
        ]
    lag = int(out.strip() or b"0")
    if lag <= seam.allowed_lag:
        return []
    shown = ", ".join(stale[:6]) + (f" and {len(stale) - 6} more" if len(stale) > 6 else "")
    return [
        Finding(
            seam.id,
            "stale",
            f"{len(stale)} file(s) differ from {seam.producer} at {ref}, {lag} commit(s) behind "
            f"(allowed {seam.allowed_lag}): {shown}",
        )
    ]


def _contract_copy(seam: Seam, roots: dict[str, Path], ref: str, read: Reader) -> list[Finding]:
    """A vendored schema against the producer's: what constrains data, not wording."""
    upstream = read(seam.producer, seam.schema, ref)
    if upstream is None:
        return [Finding(seam.id, "no-schema", f"{seam.schema} is not in {seam.producer} at {ref}")]
    try:
        ours = json.loads((roots[seam.consumer] / seam.copy_path).read_text("utf-8"))
        theirs = json.loads(upstream)
    except (OSError, ValueError) as err:
        return [Finding(seam.id, "unchecked", f"a schema could not be read: {err}")]
    if _contract(ours) == _contract(theirs):
        return []
    return [
        Finding(
            seam.id,
            "stale",
            f"{seam.copy_path} differs from {seam.producer}'s {seam.schema} at {ref}",
        )
    ]


def _one(seam: Seam, roots: dict[str, Path], refs: dict[str, str], read: Reader) -> list[Finding]:
    """Judge one row that names this check; the other rows are not this check's."""
    if seam.freshness != CHECK_ID or seam.copy_path == "-":
        return []
    missing = [s for s in (seam.producer, seam.consumer) if s not in roots]
    if missing:
        return [Finding(seam.id, "unchecked", f"no checkout of {', '.join(missing)}")]
    if not (roots[seam.consumer] / seam.copy_path).exists():
        return [Finding(seam.id, "missing-copy", f"{seam.consumer} has no {seam.copy_path}")]
    ref = refs[seam.producer]
    if seam.kind == "vendor":
        return _vendor(seam, roots, ref, read)
    if seam.kind == "contract":
        return _contract_copy(seam, roots, ref, read)
    return []


def scan_copies(tracked: Iterable[str]) -> list[str]:
    """The vendored copies among a repo's tracked paths: manifests, ``vendor/<x>/`` and fixture schemas (pure)."""
    units: set[str] = set()
    for path in tracked:
        parts = path.split("/")
        if "tests" in parts[:-1] or "test" in parts[:-1]:
            continue
        if parts[-1] == "MANIFEST.tsv":
            units.add("/".join(parts[:-1]) + "/" if len(parts) > 1 else "./")
        elif len(parts) > 2 and parts[0] == "vendor":
            units.add(f"vendor/{parts[1]}/")
        elif parts[-1].endswith(".schema.json") and (
            "fixtures" in parts[:-1] or "vendor" in parts[:-1]
        ):
            units.add(path)
    return sorted(units)


def _covered(unit: str, copies: Sequence[str]) -> bool:
    """Whether a row's ``copy_path`` is this copy or holds it (pure)."""
    return any(unit == c or (c.endswith("/") and unit.startswith(c)) for c in copies)


def unregistered(seams: Sequence[Seam], system: str, tracked: Iterable[str]) -> list[Finding]:
    """Copies in ``system``'s tree that no row names as its ``copy_path`` (pure)."""
    copies = [s.copy_path for s in seams if s.consumer == system and s.copy_path != "-"]
    return [
        Finding(
            f"copy:{unit}",
            "unregistered",
            f"{system} holds {unit} and contracts/seams.tsv has no row for it",
        )
        for unit in scan_copies(tracked)
        if not _covered(unit, copies)
    ]


def check(
    seams: Sequence[Seam],
    roots: dict[str, Path],
    read: Reader,
    refs: dict[str, str],
    tracked: dict[str, list[str]],
) -> list[Finding]:
    """Every finding over the register: stale copies, missing copies and copies with no row."""
    found: list[Finding] = []
    for seam in seams:
        found.extend(_one(seam, roots, refs, read))
    for system, paths in sorted(tracked.items()):
        found.extend(unregistered(seams, system, paths))
    return found


def _tracked(root: Path) -> list[str]:
    """Tracked paths of a checkout (empty when it is not a git tree)."""
    code, out = _git(root, "ls-files")
    return out.decode("utf-8", errors="replace").splitlines() if code == 0 else []


def main(argv: Sequence[str] | None = None) -> int:
    """Print one tab-separated finding per line. Exit 0 none, 1 findings, 3 nothing could be judged."""
    parser = argparse.ArgumentParser(
        prog="seams", description=__doc__.splitlines()[0] if __doc__ else ""
    )
    parser.add_argument(
        "--register", required=True, type=Path, help="the ambient layer's contracts/seams.tsv"
    )
    parser.add_argument(
        "--root", action="append", default=[], metavar="SYSTEM=DIR", help="a checkout, repeatable"
    )
    parser.add_argument(
        "--no-scan",
        action="append",
        default=[],
        metavar="SYSTEM",
        help="a system whose tree is the register's home: not scanned for copies",
    )
    parser.add_argument(
        "--ref",
        default="",
        help="the producer ref to compare with (default origin/main, else HEAD)",
    )
    args = parser.parse_args(argv)
    roots = {name: Path(path) for name, _, path in (r.partition("=") for r in args.root) if path}
    try:
        seams = load_register(args.register.read_text("utf-8"))
    except OSError as err:
        print(f"seams\tunchecked\tcannot read the register: {err}")
        return 3
    refs = {name: args.ref or default_ref(path) for name, path in roots.items()}
    tracked = {name: _tracked(path) for name, path in roots.items() if name not in args.no_scan}
    found = check(seams, roots, git_reader(roots), refs, tracked)
    for finding in found:
        print(finding.line())
    if found and all(f.kind == "unchecked" for f in found) and len(found) >= len(seams):
        return 3
    return 1 if any(f.kind != "unchecked" for f in found) else 0


if __name__ == "__main__":
    sys.exit(main())

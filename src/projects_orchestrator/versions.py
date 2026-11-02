"""Where does each repo's pinned version differ from the box register? (box register, versions).

The ambient layer's ``standards/naming/versions.tsv`` holds the Python floor, the interpreter
file, the tools, the libraries and every GitHub Action's commit. This reads a repo's
``pyproject.toml``, ``.python-version``, ``package.json`` and workflows and says,
per pin, whether it matches. A difference is drift. Drift is either fixed or a row
in the exception file with a reason; a repo that is out of scope entirely gets one
``*`` row. An action a workflow pins that the register does not name is drift too.

Pure where it can be: :func:`inspect` reads files, :func:`drift` compares facts with
the register, :func:`classify` applies the exceptions. Never raises on an unreadable
file: that pin is absent, which is not drift.

The exception file names repositories, so it is local to the box (the private layer),
never committed to this public repo. Update-bot coverage is reported per repo.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

_SHA = re.compile(r"^[0-9a-f]{40}$")
_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*([^\s#]+)")
_BUN = re.compile(r"^\s*bun-version:\s*([^\s#]+)")
_SPEC = re.compile(
    r"^\s*[\"']?([A-Za-z0-9_.-]+)\s*(?:\[[^\]]*\])?\s*((?:>=|==|~=|>|<=|<|!=)\s*[0-9][0-9.]*)?"
)
_BOTS = (
    "renovate.json",
    ".github/renovate.json",
    "renovate.json5",
    ".renovaterc",
    ".renovaterc.json",
    ".github/dependabot.yml",
)
_PY_TOOLS = ("ruff", "mypy", "pytest")


@dataclass(frozen=True)
class Pin:
    """One row of the register.

    Attributes:
        id: ``<axis>.<name>``.
        axis: ``runtime`` | ``tool`` | ``library`` | ``action``.
        name: What is pinned.
        value: The registered version.
        sha: The action's commit, ``-`` otherwise.
    """

    id: str
    axis: str
    name: str
    value: str
    sha: str


@dataclass(frozen=True)
class Drift:
    """One pin of one repo that differs from the register.

    Attributes:
        repo: The repo's name.
        id: The register row, or ``action:<name>`` for an unregistered action, or ``update-bot``.
        found: What the repo pins.
        expected: What the register holds.
        status: ``drift`` | ``excepted`` | ``exempt`` | ``ok`` (an update bot is present).
        reason: The exception row's reason; empty for drift.
    """

    repo: str
    id: str
    found: str
    expected: str
    status: str = "drift"
    reason: str = ""

    def line(self) -> str:
        """The finding as one tab-separated line."""
        return "\t".join((self.repo, self.id, self.status, self.found, self.expected, self.reason))


@dataclass
class Facts:
    """What a repo pins, read from its files.

    Attributes:
        python_floor: The ``requires-python`` lower bound, ``X.Y``, or empty.
        python_file: The ``.python-version`` as ``X.Y``, or empty.
        specs: Tool and library name to its declared lower bound (``unpinned`` when none).
        bun: The ``bun-version`` a workflow gives, or empty.
        actions: ``(name, ref, workflow)`` for every non-local ``uses:``.
        bot: The update bot's config path, or empty.
    """

    python_floor: str = ""
    python_file: str = ""
    specs: dict[str, str] = field(default_factory=dict)
    bun: str = ""
    actions: list[tuple[str, str, str]] = field(default_factory=list)
    bot: str = ""


def load_register(text: str) -> list[Pin]:
    """Parse ``versions.tsv`` text into rows, skipping comments and the header (pure)."""
    rows: list[Pin] = []
    for raw in text.splitlines():
        cells = raw.split("\t")
        if raw and not raw.startswith("#") and cells[0] != "id" and len(cells) >= 5:
            rows.append(Pin(cells[0], cells[1], cells[2], cells[3], cells[4]))
    return rows


def _mm(version: str) -> str:
    """``3.13.2`` -> ``3.13`` (pure)."""
    return ".".join(version.split(".")[:2])


def _read(path: Path) -> str:
    """A file's text, or empty when it cannot be read."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        # expected: an unreadable file pins nothing, which is not drift
        return ""


def _dependency_strings(pyproject: dict[str, object]) -> list[str]:
    """Every requirement string a pyproject declares: project, optional and dependency groups (pure)."""
    found: list[str] = []
    project = pyproject.get("project")
    if isinstance(project, dict):
        found += [d for d in project.get("dependencies", []) if isinstance(d, str)]
        for group in (project.get("optional-dependencies") or {}).values():
            found += [d for d in group if isinstance(d, str)]
    for group in (pyproject.get("dependency-groups") or {}).values():  # type: ignore[attr-defined]
        found += [d for d in group if isinstance(d, str)]
    return found


def _specs(requirements: Iterable[str]) -> dict[str, str]:
    """Registered tools' declared lower bounds from requirement strings (pure)."""
    out: dict[str, str] = {}
    for req in requirements:
        match = _SPEC.match(req)
        if match and match.group(1).lower() in _PY_TOOLS:
            spec = (match.group(2) or "").replace(" ", "")
            out[match.group(1).lower()] = (
                spec.lstrip("<>=!~") if spec.startswith((">=", "==", "~=", ">")) else "unpinned"
            )
    return out


def inspect(root: Path) -> Facts:
    """Read what a checkout pins."""
    facts = Facts()
    try:
        pyproject = tomllib.loads(_read(root / "pyproject.toml"))
    except tomllib.TOMLDecodeError:
        # expected: an unparseable pyproject pins nothing readable
        pyproject = {}
    requires = str((pyproject.get("project") or {}).get("requires-python", ""))
    floor = re.search(r">=\s*([0-9]+\.[0-9]+)", requires)
    facts.python_floor = floor.group(1) if floor else ""
    facts.python_file = _mm(_read(root / ".python-version").strip())
    facts.specs = _specs(_dependency_strings(pyproject))
    try:
        package = json.loads(_read(root / "package.json") or "{}")
    except ValueError:
        # expected: an unparseable package.json pins nothing readable
        package = {}
    for section in ("dependencies", "devDependencies"):
        if isinstance(package.get(section), dict) and "typescript" in package[section]:
            facts.specs["typescript"] = str(package[section]["typescript"])
    for workflow in sorted((root / ".github" / "workflows").glob("*.y*ml")):
        for line in _read(workflow).splitlines():
            if m := _USES.match(line):
                name, _, ref = m.group(1).strip("\"'").partition("@")
                if ref and not name.startswith(("./", "docker:")):
                    facts.actions.append((name, ref, workflow.name))
            if b := _BUN.match(line):
                facts.bun = b.group(1).strip("\"'")
    facts.bot = next((bot for bot in _BOTS if (root / bot).is_file()), "")
    return facts


def _version_key(version: str) -> tuple[int, ...]:
    """A dotted version as a comparable tuple; non-numeric parts count as 0 (pure)."""
    return tuple(
        int(p) if p.isdigit() else 0 for p in re.split(r"[.^~]", version.lstrip("^~>=v")) if p != ""
    )


def _runtime(repo: str, pins: dict[str, Pin], facts: Facts) -> list[Drift]:
    """The interpreter pins: exact equality, absent is not drift (pure)."""
    out: list[Drift] = []
    for key, found in (
        ("runtime.python-floor", facts.python_floor),
        ("runtime.python-version-file", facts.python_file),
    ):
        pin = pins.get(key)
        if pin and found and found != pin.value:
            out.append(Drift(repo, key, found, pin.value))
    return out


def _tools(repo: str, pins: dict[str, Pin], facts: Facts) -> list[Drift]:
    """Tool and library floors: a declared bound below the register, or no bound, is drift (pure)."""
    out: list[Drift] = []
    for pin in pins.values():
        found = facts.specs.get(pin.name)
        if pin.axis not in ("tool", "library") or found is None:
            continue
        bare = found.lstrip("^~>=v")
        if found == "unpinned" or _version_key(bare) < _version_key(pin.value):
            out.append(Drift(repo, pin.id, found, f">={pin.value}"))
        elif pin.name == "typescript" and _version_key(bare)[:1] != _version_key(pin.value)[:1]:
            out.append(Drift(repo, pin.id, found, f"^{pin.value}"))
    bun = pins.get("tool.bun")
    if bun and facts.bun and (facts.bun == "latest" or _mm(facts.bun) != bun.value):
        out.append(Drift(repo, bun.id, facts.bun, bun.value))
    return out


def _actions(repo: str, pins: dict[str, Pin], facts: Facts) -> list[Drift]:
    """Every pinned action against the register: a mutable tag, another commit, or no row (pure)."""
    by_name = {p.name: p for p in pins.values() if p.axis == "action"}
    out: dict[tuple[str, str], Drift] = {}
    for name, ref, _ in facts.actions:
        pin = by_name.get(name)
        if pin is None:
            out[(name, "")] = Drift(repo, f"action:{name}", ref[:7], "no row in the register")
        elif not _SHA.match(ref):
            out[(name, ref)] = Drift(
                repo, pin.id, f"@{ref} (mutable tag)", f"{pin.sha[:7]} {pin.value}"
            )
        elif ref != pin.sha:
            out[(name, ref)] = Drift(repo, pin.id, ref[:7], f"{pin.sha[:7]} {pin.value}")
    return list(out.values())


def drift(repo: str, pins: Sequence[Pin], facts: Facts) -> list[Drift]:
    """Every difference between a repo's pins and the register, update-bot absence included (pure)."""
    by_id = {p.id: p for p in pins}
    found = _runtime(repo, by_id, facts) + _tools(repo, by_id, facts) + _actions(repo, by_id, facts)
    if facts.bot:
        found.append(Drift(repo, "update-bot", facts.bot, "renovate or dependabot", "ok"))
    else:
        found.append(Drift(repo, "update-bot", "none", "renovate or dependabot"))
    return found


def load_exceptions(text: str) -> list[tuple[str, str, str]]:
    """``(repo, id, reason)`` rows of the exception file; ``*`` for a whole repo or every axis (pure)."""
    rows = []
    for raw in text.splitlines():
        cells = raw.split("\t")
        if raw and not raw.startswith("#") and cells[0] != "repo" and len(cells) >= 3:
            rows.append((cells[0], cells[1], cells[2]))
    return rows


def classify(found: Sequence[Drift], exceptions: Sequence[tuple[str, str, str]]) -> list[Drift]:
    """Mark each drift ``excepted`` or ``exempt`` when an exception row covers it (pure)."""
    out: list[Drift] = []
    for item in found:
        status, reason = item.status, ""
        for repo, ident, why in exceptions:
            if item.status == "drift" and repo == item.repo and ident in ("*", item.id):
                status, reason = ("exempt" if ident == "*" else "excepted"), why
                break
        out.append(Drift(item.repo, item.id, item.found, item.expected, status, reason))
    return out


def report(
    repos: dict[str, Path], pins: Sequence[Pin], exceptions: Sequence[tuple[str, str, str]]
) -> list[Drift]:
    """Drift for every repo, classified. A repo with a ``*`` exception is listed once as exempt."""
    exempt = {repo: why for repo, ident, why in exceptions if ident == "*"}
    out: list[Drift] = []
    for name, root in sorted(repos.items()):
        if name in exempt:
            out.append(Drift(name, "*", "-", "-", "exempt", exempt[name]))
            continue
        out += classify(drift(name, pins, inspect(root)), exceptions)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    """Print one line per drift and a count; exit 1 when any drift has no exception, else 0."""
    parser = argparse.ArgumentParser(
        prog="versions", description="Each repo's drift from the versions register."
    )
    parser.add_argument(
        "--register",
        required=True,
        type=Path,
        help="the ambient layer's standards/naming/versions.tsv",
    )
    parser.add_argument(
        "--exceptions", type=Path, help="the box-local exception file: repo, id, reason"
    )
    parser.add_argument(
        "--repo", action="append", default=[], metavar="NAME=DIR", help="a checkout, repeatable"
    )
    args = parser.parse_args(argv)
    try:
        pins = load_register(args.register.read_text("utf-8"))
    except OSError as err:
        print(f"versions\tunchecked\tcannot read the register: {err}")
        return 3
    exceptions = load_exceptions(_read(args.exceptions)) if args.exceptions else []
    repos = {n: Path(p) for n, _, p in (r.partition("=") for r in args.repo) if p}
    rows = report(repos, pins, exceptions)
    for row in rows:
        print(row.line())
    open_drift = [r for r in rows if r.status == "drift"]
    counts = {
        s: sum(1 for r in rows if r.status == s) for s in ("drift", "excepted", "exempt", "ok")
    }
    print(
        f"# {counts['drift']} drift, {counts['excepted']} excepted, {counts['exempt']} exempt repo(s)"
    )
    return 1 if open_drift else 0


if __name__ == "__main__":
    sys.exit(main())

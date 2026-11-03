"""#248: nothing this repo ships or publishes names the private upstream it serves.

This repo is public, and the wheel goes wherever `pip install` takes it. The
`--json` seam and the descriptor's marker rules are consumed by a private
ambient layer, and describing that seam is exactly when its name slips in: the
text reads as ordinary technical prose written by someone with both repos open.
Nothing fails and nothing warns, so the boundary is kept by this test or not at
all. Ported from project-init, which has had the same guard since PI-949.

SCOPE IS DERIVED, NOT LISTED. The shipped set is read out of `pyproject.toml`
at test time — `packages`, `force-include` and the readme — because project-init
enumerated its shipped paths by hand and the list drifted twice: `schemas/` was
force-included into the wheel, picked up a private citation, and would have been
published with that test green (PI-989). A list of shipped paths that disagrees
with the build config is a second copy of a fact.

The scan is deliberately WIDER than the wheel: every file git tracks, because
the repository is public and read by anyone who opens it. The shipped set is
scanned on top of that from the working tree, so a packaged file git does not
track (a generated module, say) is still covered.

Rewording is the remedy, not deletion: the seam is real and its description
must survive. The layer is named by role ("the ambient layer", "the consumer"),
and the marker contract's case ids (H1, M13, M24) are kept, because both sibling
readers assert the same fixtures by those ids.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Lowercased substrings that name the private system. A list so a second name
# can be added without reshaping the test.
_PRIVATE_NAMES = ["harbor"]

# Paths the BUILD CONFIG ships that the scan deliberately does not read, each
# with its reason. Held explicitly rather than by omission, so an exemption is a
# visible decision; one naming a path the build no longer ships fails below.
_SCAN_EXEMPT: dict[str, str] = {}

# The only file allowed to carry the name: it has to spell it to search for it.
# Held as a path relative to the tree, because under mutmut the suite runs from
# a `mutants/` copy while git lists the original, so there are two of it.
_SELF = Path(__file__).resolve().relative_to(_REPO_ROOT)

_SKIP_SUFFIXES = {".pyc", ".png", ".svg", ".ico", ".lock"}

# A verbatim copy of the naming standard's package: the MANIFEST row hashes of every file in it are
# held by `just docs-check`, so rewording a line is an edit the gate refuses. Nothing else may name it.
_VENDORED_PREFIX = "vendor/naming/"


def _shipped() -> set[str]:
    """Every path `pyproject.toml` puts in the wheel, relative to the repo root."""
    cfg = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = (
        cfg.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
    )
    ships: set[str] = set(wheel.get("packages", [])) | set(wheel.get("force-include", {}))
    readme = cfg.get("project", {}).get("readme")
    if isinstance(readme, str):
        ships.add(readme)
    elif isinstance(readme, dict) and isinstance(readme.get("file"), str):
        ships.add(readme["file"])
    return ships


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def _top() -> Path:
    """The work tree's top level, asked of git rather than assumed: the suite
    also runs inside mutmut's untracked `mutants/` copy, where `git ls-files`
    lists nothing."""
    return Path(_git("rev-parse", "--show-toplevel", cwd=_REPO_ROOT).strip())


def _tracked() -> list[Path]:
    top = _top()
    return [top / line for line in _git("ls-files", cwd=top).splitlines() if line]


def _shipped_roots() -> list[Path]:
    return [_REPO_ROOT / root for root in sorted(_shipped() - set(_SCAN_EXEMPT))]


def _is_exempt(path: Path) -> bool:
    return any(path.is_relative_to(_REPO_ROOT / root) for root in _SCAN_EXEMPT)


def _is_vendored(path: Path) -> bool:
    return any(path.is_relative_to(base / _VENDORED_PREFIX) for base in {_REPO_ROOT, _top()})


def _scanned_files() -> list[Path]:
    selves = {_REPO_ROOT / _SELF, _top() / _SELF}
    candidates = set(_tracked())
    for root in _shipped_roots():
        candidates.update([root] if root.is_file() else root.rglob("*"))
    return sorted(
        p
        for p in candidates
        if p.is_file()
        and p.suffix not in _SKIP_SUFFIXES
        and "__pycache__" not in p.parts
        and p not in selves
        and not _is_exempt(p)
        and not _is_vendored(p)
    )


@pytest.mark.reads_the_checkout
@pytest.mark.parametrize("name", _PRIVATE_NAMES)
def test_nothing_shipped_or_public_names_the_private_upstream(name: str) -> None:
    pattern = re.compile(re.escape(name), re.IGNORECASE)
    hits: list[str] = []
    for path in _scanned_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path}:{lineno}: {line.strip()[:90]}")
    assert not hits, (
        f"{len(hits)} reference(s) to the private upstream; name the layer by role instead:\n"
        + "\n".join(hits)
    )


def test_the_scan_reaches_the_files_it_claims_to() -> None:
    """A negative assertion needs a positive control, or a walk that silently
    skips everything reports green for ever. Pinned to the files the references
    actually lived in when this was filed."""
    top = _top()
    scanned = {p.relative_to(top).as_posix() for p in _scanned_files() if p.is_relative_to(top)}
    for required in (
        "src/projects_orchestrator/descriptor.py",
        "src/projects_orchestrator/digest.py",
        "README.md",
        "schemas/README.md",
        "schemas/snapshot.v1.schema.json",
        "tests/check/fleet/test_json_seam.py",
        "AGENTS.md",
        ".github/hooks/pre-push",
    ):
        assert required in scanned, f"the scan does not reach {required}"
    assert len(scanned) > 100, f"only {len(scanned)} files scanned — the walk is not working"


def test_every_shipped_root_exists() -> None:
    """A shipped root that is not there is scanned as nothing: `rglob` over a
    missing directory yields no files rather than raising."""
    for root in _shipped_roots():
        assert root.exists(), f"shipped root {root} does not exist"
        assert root.is_file() or any(root.rglob("*")), f"shipped root {root} is empty"


def test_the_shipped_set_is_read_and_every_exemption_is_still_shipped() -> None:
    """The shipped set comes from the build config, never from this file.

    An empty parse means the reader is wrong, not the config — without this the
    scan would quietly narrow to what git tracks. An exemption for a path the
    build no longer ships is a stale excuse that would widen the next one.
    """
    ships = _shipped()
    assert "src/projects_orchestrator" in ships, f"read {sorted(ships)} out of pyproject.toml"
    assert "README.md" in ships, "the wheel's long description is shipped too"
    stale = sorted(path for path in _SCAN_EXEMPT if path not in ships)
    assert not stale, "_SCAN_EXEMPT names paths pyproject.toml no longer ships:\n  " + "\n  ".join(
        stale
    )

"""The meta-file standard: every doc carries one front matter shape, every generated file one header.

The judges are the vendored ``doc-shape.sh`` and ``doc-fresh-verify.sh``, run through ``gates/doc-check.sh``
(``just docs-check``, part of ``just lint``). The tests run the real scripts on a copy of this tree and break
each thing the gate guards, watch it go red, and restore it:

* the tree as committed passes strict;
* a doc without front matter, a kind that disagrees with its folder and a doc with no owner are each red;
* a hand edit to a generated file (the code map, CODEOWNERS) is STALE, and regenerating makes it green;
* a ``generated-by:`` header with no row, and a row whose file lacks the header, are red.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SKIP = shutil.ignore_patterns(
    ".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", "mutants", "site"
)
_OWNER = "owner: @fixture-owner"


def _run(repo: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", *argv], cwd=repo, capture_output=True, text=True, check=False, timeout=300
    )


def _gate(repo: Path) -> subprocess.CompletedProcess[str]:
    return _run(repo, "gates/doc-check.sh", "--strict")


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One private copy of this tree, git-free so the judges read the files, not an index."""
    dest = tmp_path_factory.mktemp("docshape") / "repo"
    shutil.copytree(_ROOT, dest, ignore=_SKIP, symlinks=True)
    return dest


@pytest.fixture
def repo(tree: Path, tmp_path: Path) -> Path:
    work = tmp_path / "repo"
    shutil.copytree(tree, work, symlinks=True)
    return work


def test_the_tree_as_committed_passes_strict(repo: Path) -> None:
    r = _gate(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "carry the front matter shape" in r.stdout + r.stderr
    assert "fresh-check: 3 ok" in r.stdout + r.stderr


def test_a_doc_without_front_matter_is_red_then_green(repo: Path) -> None:
    doc = repo / "docs/how-to/bare.md"
    doc.write_text("# Bare\n", encoding="utf-8")
    red = _gate(repo)
    assert red.returncode == 1, red.stdout + red.stderr
    assert "docs/how-to/bare.md" in red.stdout + red.stderr
    doc.write_text(f"---\nkind: how-to\n{_OWNER}\nstatus: draft\n---\n\n# Bare\n", encoding="utf-8")
    green = _gate(repo)
    assert green.returncode == 0, green.stdout + green.stderr


def test_a_kind_that_disagrees_with_its_folder_is_red(repo: Path) -> None:
    (repo / "docs/how-to/wrong.md").write_text(
        f"---\nkind: explanation\n{_OWNER}\nstatus: draft\n---\n", encoding="utf-8"
    )
    red = _gate(repo)
    assert red.returncode == 1, red.stdout + red.stderr
    assert "doc.kind-folder" in red.stdout + red.stderr


def test_a_doc_with_no_owner_is_red(repo: Path) -> None:
    (repo / "docs/how-to/orphan.md").write_text(
        "---\nkind: how-to\nstatus: draft\n---\n", encoding="utf-8"
    )
    red = _gate(repo)
    assert red.returncode == 1, red.stdout + red.stderr
    assert "no owner" in red.stdout + red.stderr


@pytest.mark.parametrize(
    ("generated", "fix"),
    [
        (".agents/docs/CODE_MAP.md", ["uv", "run", "python", ".agents/scripts/gen_code_map.py"]),
        (
            ".github/CODEOWNERS",
            ["uv", "run", "python", ".agents/scripts/gen_codeowners.py", "--write"],
        ),
    ],
)
def test_a_hand_edited_generated_file_is_stale_then_fresh(
    repo: Path, generated: str, fix: list[str]
) -> None:
    path = repo / generated
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "hand edit\n", encoding="utf-8")
    red = _gate(repo)
    assert red.returncode == 1, red.stdout + red.stderr
    assert f"{generated} is STALE" in red.stdout + red.stderr
    regenerated = subprocess.run(fix, cwd=repo, capture_output=True, text=True, check=False)
    assert regenerated.returncode == 0, regenerated.stdout + regenerated.stderr
    assert path.read_text(encoding="utf-8") == original
    green = _gate(repo)
    assert green.returncode == 0, green.stdout + green.stderr


def test_a_header_with_no_row_is_red(repo: Path) -> None:
    (repo / "out.txt").write_text("# generated-by: sh make-out.sh\nx\n", encoding="utf-8")
    red = _gate(repo)
    assert red.returncode == 1, red.stdout + red.stderr
    assert "doc.fresh-registered" in red.stdout + red.stderr


def test_a_row_whose_file_lacks_the_header_is_red(repo: Path) -> None:
    table = repo / "contracts/fresh-checks.tsv"
    table.write_text(
        table.read_text(encoding="utf-8") + "out.txt\ttrue\tsh make-out.sh\t\n", encoding="utf-8"
    )
    (repo / "out.txt").write_text("x\n", encoding="utf-8")
    red = _gate(repo)
    assert red.returncode == 1, red.stdout + red.stderr
    assert "no generated-by header" in red.stdout + red.stderr

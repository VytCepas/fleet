"""The approval-gate opt-in: the register is well-formed, CODEOWNERS is derived from it,
and the paths that must wait for the maintainer do, while ordinary source does not.

An approval gate reads `.agents/ship.conf` and `.agents/protected-paths.tsv` from the
default branch. A shipped guard that slips out of the register merges unattended; a
register that protects everything makes the opt-in pointless. Both directions are pinned.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_TOOL = _ROOT / ".agents" / "scripts" / "gen_codeowners.py"

_spec = importlib.util.spec_from_file_location("gen_codeowners", _TOOL)
assert _spec and _spec.loader
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def _git(*argv: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *argv], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def _tracked(root: Path = _ROOT) -> list[str]:
    # From git's top level, not `root`: the nightly mutation run executes this suite
    # from mutmut's untracked `mutants/` copy, where `git ls-files` lists nothing.
    top = Path(_git("rev-parse", "--show-toplevel", cwd=root).strip())
    return _git("ls-files", cwd=top).splitlines()


def test_tracked_lists_the_checkout_from_inside_an_untracked_copy(tmp_path):
    _git("init", "-q", cwd=tmp_path)
    (tmp_path / ".agents" / "hooks").mkdir(parents=True)
    (tmp_path / ".agents" / "hooks" / "guard.py").write_text("x\n")
    _git("add", ".", cwd=tmp_path)
    (tmp_path / "mutants").mkdir()
    assert _tracked(tmp_path / "mutants") == [".agents/hooks/guard.py"]


def test_manifest_is_merge_only():
    assert gen.manifest_value(_ROOT, "repo") == "VytCepas/fleet"
    assert (_ROOT / ".github" / "workflows" / gen.manifest_value(_ROOT, "workflow")).is_file()
    for key in ("plan", "apply", "verify", "restore", "deployed"):
        assert gen.manifest_value(_ROOT, key) == "", f"merge-only manifest carries {key}"


def test_register_rows_are_well_formed():
    rows = gen.read_rows(_ROOT)
    assert rows, "the register protects nothing"
    globs = [g for g, _, _ in rows]
    assert len(globs) == len(set(globs)), "a glob is listed twice"


def test_required_checks_name_this_repo():
    rows = [
        line.split("\t")
        for line in (_ROOT / ".agents" / "required-checks.tsv").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert rows and all(len(r) == 2 and r[0] == "VytCepas/fleet" for r in rows)
    assert ["VytCepas/fleet", "CI gate"] in rows


def test_codeowners_is_current():
    result = subprocess.run(
        [sys.executable, str(_TOOL), "--check"], cwd=_ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_check_fails_on_a_stale_codeowners(tmp_path):
    """The check above must be able to go red: add a row and it does."""
    for rel in (gen.MANIFEST, gen.DEFAULT_REGISTER, gen.OUT):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_ROOT / rel, tmp_path / rel)
    assert gen.main(["--check", "--root", str(tmp_path)]) == 0
    with (tmp_path / gen.DEFAULT_REGISTER).open("a") as f:
        f.write("src/new_guard.py\thook\ta new guard\n")
    assert gen.main(["--check", "--root", str(tmp_path)]) == 1


def test_malformed_row_is_refused(tmp_path):
    (tmp_path / ".agents").mkdir()
    (tmp_path / gen.MANIFEST).write_text("repo a/b\n")
    (tmp_path / gen.DEFAULT_REGISTER).write_text("glob\tclass\treason\nsrc/x.py\thook\n")
    with pytest.raises(ValueError, match="glob<TAB>class<TAB>reason"):
        gen.read_rows(tmp_path)


def test_glob_semantics_match_the_gate():
    rx = gen.glob_regex("templates/**/hooks/**")
    assert rx.match("templates/base/dot_agents/hooks/prod_guard.py")
    assert rx.match("Templates/Base/dot_agents/hooks/x.sh")  # case-insensitive
    assert not gen.glob_regex("bin/*.sh").match("bin/sub/x.sh")  # * stops at /


def _shipped_guards() -> list[str]:
    hooks = [p for p in _tracked() if "/hooks/" in f"/{p}" and not p.startswith("tests/")]
    assert hooks, "found no hook files: the scan is broken"
    return hooks


@pytest.mark.parametrize(
    "path",
    [
        "CLAUDE.md",
        "AGENTS.md",
        "justfile",
        "pyproject.toml",
        ".agents/hooks/prod_guard.py",
        ".agents/scripts/install_tool.py",
        ".agents/settings.json",
        ".claude/settings.json",
        ".codex/hooks.json",
        "contrib/systemd/fleet-controller-heal.timer",
        "src/projects_orchestrator/sandbox.py",
        "src/projects_orchestrator/landing.py",
        ".agents/ship.conf",
        ".agents/protected-paths.tsv",
        ".github/CODEOWNERS",
    ],
)
def test_guards_and_governance_are_protected(path):
    assert gen.is_protected(_ROOT, path)


def test_every_hook_is_protected():
    loose = [p for p in _shipped_guards() if not gen.is_protected(_ROOT, p)]
    assert not loose, f"hooks the gate would merge unattended: {loose}"


@pytest.mark.parametrize(
    "path",
    [
        "src/projects_orchestrator/fleet.py",
        "tests/status/fleet/test_fleet.py",
        "docs/index.md",
        "README.md",
    ],
)
def test_ordinary_source_stays_unprotected(path):
    assert not gen.is_protected(_ROOT, path)

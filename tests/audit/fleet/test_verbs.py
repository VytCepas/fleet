"""The ``verbs`` audit probe: a justfile missing a verb its repo-kind requires is red (harbor#1428)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import make_project

from projects_orchestrator.audit import audit_project
from projects_orchestrator.descriptor import load_descriptor

_REGISTRY = (
    "id\tmeaning\texit\tkinds\tnote\n"
    "lint\tstatic checks\t0 clean\tworkload\t-\n"
    "test\tthe suite\t0 pass\tworkload\t-\n"
    "naming\tnaming ratchet\t0 none\tworkload\t-\n"
)
_KINDS = "id\trequired\toptional\tnote\nworkload\tjustfile\t-\tx\n"


def _judge(root: Path) -> Path:
    """A stub naming package at ``root``/vendor/naming that reads the real lint's contract.

    The real lint is harbor's; here a tiny sh script reads the same register and
    prints the records the audit parses, so the test needs no harbor checkout.
    """
    vendored = root / "vendor" / "naming"
    (vendored / "registry").mkdir(parents=True)
    (vendored / "registry" / "verbs.tsv").write_text(_REGISTRY, encoding="utf-8")
    (vendored / "registry" / "repo-kinds.tsv").write_text(_KINDS, encoding="utf-8")
    script = vendored / "lint-name.sh"
    script.write_text(
        "#!/bin/sh\n"
        "# stub of lint-name.sh --check repo-kind: A <TAB> name.repo-verb <TAB> repo-verb:<id> for each\n"
        "# register verb the root's justfile has no recipe for\n"
        'root=""; reg=""\n'
        'while [ $# -gt 0 ]; do case "$1" in --root) root="$2"; shift;; --registry) reg="$2"; shift;; esac; shift; done\n'
        "awk -F'\\t' 'NR == FNR { if ($0 ~ /^[a-z][a-z0-9-]*:/) { n = $0; sub(/:.*/, \"\", n); have[n] = 1 } next }\n"
        '  FNR > 1 { if (!($1 in have)) printf "A\\tname.repo-verb\\trepo-verb:%s\\n", $1 }\' "$root/justfile" "$reg/verbs.tsv"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    return script


def _project(fleet_dir: Path, justfile: str, kind: str | None = "workload") -> Path:
    project = make_project(fleet_dir, "alpha")
    agents = project / ".agents"
    agents.mkdir(exist_ok=True)
    if (project / ".claude" / "config.yaml").exists():
        shutil.copy(project / ".claude" / "config.yaml", agents / "config.yaml")
    if kind:
        with (agents / "config.yaml").open("a", encoding="utf-8") as handle:
            handle.write(f"repo-kind: {kind}\n")
    (project / "justfile").write_text(justfile, encoding="utf-8")
    _judge(project)
    return project


def _verbs(fleet_dir: Path) -> list:
    report = audit_project(load_descriptor(fleet_dir / "alpha"))
    return [f for f in report.findings if f.category == "verbs"]


@pytest.mark.parametrize("missing", ["test", "naming"])
def test_missing_required_verb_is_red(fleet_dir: Path, missing: str) -> None:
    recipes = {"lint", "test", "naming"} - {missing}
    _project(fleet_dir, "".join(f"{name}:\n    true\n" for name in sorted(recipes)))
    findings = _verbs(fleet_dir)
    assert [f.severity for f in findings] == ["warn"]
    assert f"missing verb: {missing}" in findings[0].message


def test_complete_justfile_is_ok(fleet_dir: Path) -> None:
    _project(fleet_dir, "lint:\n    true\ntest:\n    true\nnaming:\n    true\n")
    findings = _verbs(fleet_dir)
    assert [f.severity for f in findings] == ["ok"]


def test_no_declared_kind_is_not_judged_and_not_red(fleet_dir: Path) -> None:
    _project(fleet_dir, "", kind=None)
    findings = _verbs(fleet_dir)
    assert [f.severity for f in findings] == ["ok"]
    assert "not judged" in findings[0].message


def test_no_naming_lint_is_unchecked_not_a_pass(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(fleet_dir, "")
    shutil.rmtree(project / "vendor")
    monkeypatch.delenv("NAMING_STANDARD_REPO", raising=False)
    findings = _verbs(fleet_dir)
    assert [f.severity for f in findings] == ["warn"]
    assert "unchecked" in findings[0].message

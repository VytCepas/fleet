"""task verbs per repo kind, standard#1428: the fleet audit holds a project's justfile to the verbs its repo-kind requires (category ``verbs``).

The judge is the project's own vendored ``lint-name.sh --check repo-kind``; most tests run the package this repo vendors, against a
fixture justfile, so a missing verb is red for real. The stand-in tests speak the lint's exit-code contract (0, 1, 3).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from conftest import make_project

from projects_orchestrator.audit import audit_project
from projects_orchestrator.descriptor import load_descriptor

_PACKAGE = Path(__file__).resolve().parents[3] / "vendor" / "naming"
# the verbs verbs.tsv requires of a workload, read from the vendored register so the fixture follows it
_REGISTER = _PACKAGE / "registry" / "verbs.tsv"


def _required(kind: str) -> list[str]:
    rows = [
        line.split("\t")
        for line in _REGISTER.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and not line.startswith("id\t")
    ]
    return [row[0] for row in rows if kind in row[3].split()]


def _verbs(fleet_dir: Path, name: str = "alpha"):
    report = audit_project(load_descriptor(fleet_dir / name), None)
    found = [f for f in report.findings if f.category == "verbs"]
    assert len(found) == 1
    return found[0], report


def _declare(project: Path, kind: str) -> None:
    config = project / ".agents" / "config.yaml"
    config.write_text(config.read_text(encoding="utf-8") + f"repo-kind: {kind}\n", encoding="utf-8")


def _justfile(project: Path, recipes: list[str]) -> None:
    (project / "justfile").write_text(
        "".join(f"{r}:\n    true\n" for r in recipes), encoding="utf-8"
    )


def _vendor(project: Path) -> None:
    shutil.copytree(_PACKAGE, project / "vendor" / "naming")


def _workload(fleet_dir: Path, recipes: list[str]) -> Path:
    project = make_project(fleet_dir, "alpha", layout=".agents")
    _declare(project, "workload")
    _vendor(project)
    _justfile(project, recipes)
    return project


def test_no_declared_kind_is_not_judged(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", layout=".agents")
    finding, _ = _verbs(fleet_dir)
    assert finding.severity == "ok" and "not judged" in finding.message


def test_a_kind_without_the_lint_is_a_finding(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", layout=".agents")
    _declare(project, "workload")
    finding, _ = _verbs(fleet_dir)
    assert finding.severity == "warn" and "vendors no lint-name.sh" in finding.message


def test_a_justfile_with_every_required_verb_is_ok(fleet_dir: Path) -> None:
    _workload(fleet_dir, _required("workload"))
    finding, report = _verbs(fleet_dir)
    assert finding.severity == "ok" and "workload" in finding.message
    assert not any(f.category == "verbs" and f.severity != "ok" for f in report.findings)


def test_a_justfile_without_test_is_red(fleet_dir: Path) -> None:
    _workload(fleet_dir, [v for v in _required("workload") if v != "test"])
    finding, report = _verbs(fleet_dir)
    assert finding.severity == "warn" and "check.repo-verbs" in finding.message
    assert "test" in finding.message.split("verb(s)")[1].strip().split(", ")
    assert report.needs_attention


def test_every_missing_verb_is_named_and_extras_are_ignored(fleet_dir: Path) -> None:
    keep = [v for v in _required("workload") if v not in ("lint", "sbom")] + ["not-a-register-verb"]
    _workload(fleet_dir, keep)
    finding, _ = _verbs(fleet_dir)
    assert "lint" in finding.message and "sbom" in finding.message
    assert "not-a-register-verb" not in finding.message


def test_a_repo_exemption_row_silences_that_verb_and_only_that_verb(fleet_dir: Path) -> None:
    """The repo's own ``naming-exemptions.tsv`` (rule ``name.repo-verb``, subject ``repo-verb:<id>``) is its exemption."""
    project = _workload(fleet_dir, [v for v in _required("workload") if v not in ("fuzz", "scan")])
    (project / "naming-exemptions.tsv").write_text(
        "# exemptions\nid\trule\tsubject\tkind\tremove_by\treason\n"
        "alpha-0001\tname.repo-verb\trepo-verb:fuzz\ttool-required\t-\ta fixture exemption\n",
        encoding="utf-8",
    )
    finding, _ = _verbs(fleet_dir)
    assert finding.severity == "warn"
    assert "scan" in finding.message and "fuzz" not in finding.message


def test_an_unchecked_exit_is_never_a_pass(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", layout=".agents")
    _declare(project, "workload")
    tool = project / "vendor" / "naming" / "lint-name.sh"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    finding, _ = _verbs(fleet_dir)
    assert finding.severity == "warn" and "unchecked" in finding.message


def test_a_scaffolder_is_judged_by_its_templates_copy(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", layout=".agents")
    _declare(project, "workload")
    shutil.copytree(_PACKAGE, project / "templates" / "base" / "vendor" / "naming")
    _justfile(project, [v for v in _required("workload") if v != "test"])
    finding, _ = _verbs(fleet_dir)
    assert finding.severity == "warn" and "test" in finding.message


def test_a_lint_that_cannot_run_does_not_raise(fleet_dir: Path, monkeypatch) -> None:
    _workload(fleet_dir, _required("workload"))

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("no sh")

    monkeypatch.setattr("projects_orchestrator.audit.subprocess.run", _boom)
    finding, _ = _verbs(fleet_dir)
    assert finding.severity == "warn" and "could not run" in finding.message

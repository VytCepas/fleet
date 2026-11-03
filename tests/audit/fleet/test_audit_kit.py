"""the standard kit, #1436: the fleet audit holds a project to the kit its repo-kind inherits (category ``kit``).

The reader of the kit is the project's own vendored ``standard-kit.sh``; these tests give the project a stand-in that speaks
its exit-code contract (0 carries, 1 a ``kit:`` line per missing item, 3 unchecked), so fleet's side of the seam is what is judged.
"""

from __future__ import annotations

from pathlib import Path

from conftest import make_project

from projects_orchestrator.audit import audit_project
from projects_orchestrator.descriptor import load_descriptor


def _kit(fleet_dir: Path, name: str = "alpha"):
    report = audit_project(load_descriptor(fleet_dir / name), None)
    found = [f for f in report.findings if f.category == "kit"]
    assert len(found) == 1
    return found[0]


def _declare(project: Path, kind: str) -> None:
    config = project / ".agents" / "config.yaml"
    config.write_text(config.read_text(encoding="utf-8") + f"repo-kind: {kind}\n", encoding="utf-8")


def _tool(project: Path, body: str) -> None:
    tool = project / "vendor" / "naming" / "standard-kit.sh"
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_text(body, encoding="utf-8")


def test_no_declared_kind_is_not_judged(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", layout=".agents")
    finding = _kit(fleet_dir)
    assert finding.severity == "ok" and "not judged" in finding.message


def test_a_kind_without_a_reader_is_a_finding(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", layout=".agents")
    _declare(project, "workload")
    finding = _kit(fleet_dir)
    assert finding.severity == "warn" and "vendors no standard-kit.sh" in finding.message


def test_a_repo_that_carries_its_kit_is_ok(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", layout=".agents")
    _declare(project, "workload")
    _tool(project, "#!/bin/sh\nexit 0\n")
    finding = _kit(fleet_dir)
    assert finding.severity == "ok" and "workload" in finding.message


def test_a_missing_item_is_named_in_the_finding(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", layout=".agents")
    _declare(project, "workload")
    _tool(
        project,
        "#!/bin/sh\n"
        "echo '  kit: template.justfile lacks justfile' >&2\n"
        "echo '  kit: register.versions lacks vendor/naming/registry/versions.tsv' >&2\n"
        "exit 1\n",
    )
    finding = _kit(fleet_dir)
    assert finding.severity == "warn"
    assert (
        "template.justfile lacks justfile" in finding.message
        and "register.versions" in finding.message
    )


def test_more_than_three_missing_are_counted_not_listed(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", layout=".agents")
    _declare(project, "data")
    _tool(
        project,
        "#!/bin/sh\n"
        + "".join(f"echo '  kit: item.{i} lacks f{i}' >&2\n" for i in range(5))
        + "exit 1\n",
    )
    assert "(+2 more)" in _kit(fleet_dir).message


def test_an_unchecked_exit_is_never_a_pass(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", layout=".agents")
    _declare(project, "workload")
    _tool(project, "#!/bin/sh\nexit 3\n")
    finding = _kit(fleet_dir)
    assert finding.severity == "warn" and "unchecked" in finding.message


def test_a_reader_that_cannot_run_does_not_raise(fleet_dir: Path, monkeypatch) -> None:
    project = make_project(fleet_dir, "alpha", layout=".agents")
    _declare(project, "workload")
    _tool(project, "#!/bin/sh\nexit 0\n")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("no sh")

    monkeypatch.setattr("projects_orchestrator.audit.subprocess.run", _boom)
    finding = _kit(fleet_dir)
    assert finding.severity == "warn" and "could not run" in finding.message

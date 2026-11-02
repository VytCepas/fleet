"""Contract-v1 conformance diagnosis: manageability findings, never raise."""

from __future__ import annotations

from pathlib import Path

from conftest import make_project, make_project_v2

from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.doctor import Finding, diagnose


def _report(fleet_dir: Path, name: str = "alpha"):
    return diagnose(load_descriptor(fleet_dir / name))


def _finding(report, check: str) -> Finding:
    return next(f for f in report.findings if f.check == check)


def test_diagnose_config_ok_for_clean_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert _finding(_report(fleet_dir), "config").status == "ok"


def test_diagnose_config_fail_on_empty_config(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", config_text="")
    assert _finding(_report(fleet_dir), "config").status == "fail"


def test_diagnose_contract_ok_for_clean_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert _finding(_report(fleet_dir), "contract").status == "ok"


def test_diagnose_contract_fail_without_version(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", config_text="project:\n  name: alpha\n")
    assert _finding(_report(fleet_dir), "contract").status == "fail"


def test_diagnose_contract_ok_for_v2_project(fleet_dir: Path) -> None:
    make_project(
        fleet_dir,
        "alpha",
        config_text="project:\n  name: alpha\n  project_init_contract_version: 2\n",
    )
    assert _finding(_report(fleet_dir), "contract").status == "ok"


def test_diagnose_contract_warns_on_newer_than_understood(fleet_dir: Path) -> None:
    make_project(
        fleet_dir,
        "alpha",
        config_text="project:\n  name: alpha\n  project_init_contract_version: 9\n",
    )
    finding = _finding(_report(fleet_dir), "contract")
    assert finding.status == "warn"
    assert "newer than understood" in finding.detail


def test_diagnose_scaffold_ok_for_clean_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert _finding(_report(fleet_dir), "scaffold").status == "ok"


def test_diagnose_scaffold_warn_when_version_unknown(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", config_text="project:\n  name: alpha\n")
    assert _finding(_report(fleet_dir), "scaffold").status == "warn"


def test_diagnose_manifest_warn_without_manifest(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert _finding(_report(fleet_dir), "manifest").status == "warn"


def test_diagnose_hooks_ok_when_none_shipped(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert _finding(_report(fleet_dir), "hooks").status == "ok"


def test_diagnose_hooks_warns_on_a_stale_hook(fleet_dir: Path) -> None:
    # #241: `ok` before, because both files existed.
    project = make_project(fleet_dir, "alpha")
    source = project / ".github" / "hooks"
    source.mkdir(parents=True)
    (source / "pre-commit").write_text("#!/bin/sh\n# fixed\n", encoding="utf-8")
    installed = project / ".git" / "hooks"
    installed.mkdir(parents=True)
    (installed / "pre-commit").write_text("#!/bin/sh\n# unfixed\n", encoding="utf-8")
    finding = _finding(_report(fleet_dir), "hooks")
    assert (finding.status, finding.detail) == ("warn", "git hooks stale — run install_hooks.sh")


def test_diagnose_tooling_ok_with_declared_command(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    assert _finding(_report(fleet_dir), "tooling").status == "ok"


def test_diagnose_tooling_warn_without_commands(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={})
    assert _finding(_report(fleet_dir), "tooling").status == "warn"


def test_diagnose_upgrade_warn_without_workflow(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert _finding(_report(fleet_dir), "upgrade").status == "warn"


def test_diagnose_upgrade_ok_with_workflow(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    workflow = project / ".github/workflows/project-init-upgrade.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: upgrade\n", encoding="utf-8")
    assert _finding(_report(fleet_dir), "upgrade").status == "ok"


def test_diagnose_cloud_ok_for_library_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    finding = _finding(_report(fleet_dir), "cloud")
    assert finding.status == "ok"
    assert "no runtime probe expected" in finding.detail


def test_diagnose_cloud_ok_for_prototype_project(fleet_dir: Path) -> None:
    config = (
        "project:\n  name: alpha\n  project_init_contract_version: 1\n"
        "language: python\n"
        "delivery: prototype\n"
    )
    make_project(fleet_dir, "alpha", config_text=config)
    finding = _finding(_report(fleet_dir), "cloud")
    assert finding.status == "ok"
    assert "no runtime probe expected" in finding.detail


def test_diagnose_cloud_warns_for_v1_service_without_deploy_metadata(
    fleet_dir: Path,
) -> None:
    config = (
        "project:\n  name: alpha\n  project_init_contract_version: 1\n"
        "language: python\n"
        "delivery: service\n"
    )
    make_project(fleet_dir, "alpha", config_text=config)
    finding = _finding(_report(fleet_dir), "cloud")
    assert finding.status == "warn"
    assert "no deploy metadata" in finding.detail


def test_diagnose_cloud_warns_for_v2_service_without_deploy_block(
    fleet_dir: Path,
) -> None:
    config = (
        "project:\n  name: alpha\n  project_init_contract_version: 2\n"
        "language: python\n"
        "delivery: service\n"
    )
    make_project(fleet_dir, "alpha", config_text=config)
    finding = _finding(_report(fleet_dir), "cloud")
    assert finding.status == "warn"
    assert "contract-v2 deploy block" in finding.detail


def test_diagnose_cloud_warns_for_v2_service_with_none_target(fleet_dir: Path) -> None:
    make_project_v2(fleet_dir, "alpha", deploy_target="none")
    finding = _finding(_report(fleet_dir), "cloud")
    assert finding.status == "warn"
    assert "cloud-status cannot probe" in finding.detail


def test_diagnose_cloud_warns_for_cloud_run_missing_region(fleet_dir: Path) -> None:
    config = (
        "project:\n  name: alpha\n  project_init_contract_version: 2\n"
        "language: python\n"
        "delivery: service\n"
        "deploy:\n"
        "  target: cloud-run\n"
        "  app: alpha-svc\n"
    )
    make_project(fleet_dir, "alpha", config_text=config)
    finding = _finding(_report(fleet_dir), "cloud")
    assert finding.status == "warn"
    assert "app and region" in finding.detail


def test_diagnose_cloud_warns_for_unsupported_deploy_target(fleet_dir: Path) -> None:
    make_project_v2(fleet_dir, "alpha", deploy_target="k8s")
    finding = _finding(_report(fleet_dir), "cloud")
    assert finding.status == "warn"
    assert "not supported by cloud-status" in finding.detail


def test_diagnose_cloud_ok_for_v2_service_with_deploy_metadata(fleet_dir: Path) -> None:
    make_project_v2(fleet_dir, "alpha", deploy_target="cloud-run")
    finding = _finding(_report(fleet_dir), "cloud")
    assert finding.status == "ok"
    assert "cloud-run deploy metadata present" in finding.detail


def test_report_status_is_fail_when_any_finding_fails(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", config_text="project:\n  name: alpha\n")
    assert _report(fleet_dir).status == "fail"


def test_report_status_is_warn_when_only_warnings(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert _report(fleet_dir).status == "warn"


# --- doctor distinguishes absent from present-but-unreadable (#216) ---

_VERSION_CONFIG = """\
project:
  name: "alpha"
  description: "test project"
  project_init_version: 0.5.2
{version_line}
language: python
delivery: library
memory:
  tier: 0
tooling:
  lint_command: "true"
"""


def _with_version(fleet_dir: Path, name: str, version_line: str):
    make_project(fleet_dir, name, config_text=_VERSION_CONFIG.format(version_line=version_line))
    return diagnose(load_descriptor(fleet_dir / name))


def test_contract_finding_names_a_malformed_version_as_present(fleet_dir: Path) -> None:
    # Was: "no project_init_contract_version — predates the contract", sending
    # the operator to add a field that is already there (#216).
    report = _with_version(fleet_dir, "a", '  project_init_contract_version: "two"')
    assert "present but unreadable" in _finding(report, "contract").detail


def test_contract_finding_still_reports_a_genuinely_absent_version(fleet_dir: Path) -> None:
    # The control: absence keeps a message that is true of it. It shares that
    # message with an explicit 0 on purpose (#221): both mean v0.
    report = _with_version(fleet_dir, "b", "")
    assert _finding(report, "contract").detail.startswith("project_init_contract_version absent")


def test_contract_finding_does_not_call_an_explicit_zero_absent(fleet_dir: Path) -> None:
    # #221: a present `0` read "no project_init_contract_version", which is false.
    finding = _finding(
        _with_version(fleet_dir, "z", "  project_init_contract_version: 0"), "contract"
    )
    assert finding.status == "fail"
    assert not finding.detail.startswith("no ")
    assert "absent or 0" in finding.detail


def test_contract_finding_does_not_call_a_negative_version_absent(fleet_dir: Path) -> None:
    report = _with_version(fleet_dir, "c", "  project_init_contract_version: -3")
    assert "negative" in _finding(report, "contract").detail


def test_config_finding_fails_on_a_malformed_version(fleet_dir: Path) -> None:
    # The operator-visible path: the config check names the offending field.
    report = _with_version(fleet_dir, "d", '  project_init_contract_version: "two"')
    assert _finding(report, "config").status == "fail"


def test_doctor_fails_a_tier_that_contradicts_its_stack(fleet_dir: Path) -> None:
    """#257 (project-init #960): the edited child printed `[ok]`."""
    text = (
        "project:\n  name: edited\n  project_init_contract_version: 2\n"
        "memory:\n  tier: 0\n  stack: obsidian-graphify\n  memory_path: .agents/memory\n"
    )
    make_project(fleet_dir, "edited", config_text=text, layout=".agents")
    config = next(f for f in _report(fleet_dir, "edited").findings if f.check == "config")
    assert config.status == "fail"
    assert "disagrees with memory.stack 'obsidian-graphify'" in config.detail


def test_diagnose_marker_warns_on_the_legacy_context_key(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", config_text="context: repo\nproject:\n  name: alpha\n")
    finding = _finding(_report(fleet_dir), "marker")
    assert finding.status == "warn" and "scope:" in finding.detail


def test_diagnose_marker_ok_on_scope_and_when_undeclared(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", config_text="scope: project\nproject:\n  name: alpha\n")
    make_project(fleet_dir, "beta", config_text="project:\n  name: beta\n")
    assert _finding(_report(fleet_dir, "alpha"), "marker").status == "ok"
    assert _finding(_report(fleet_dir, "beta"), "marker").status == "ok"

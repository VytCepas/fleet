"""CLI surface: subcommands drive the engine end-to-end."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import (
    add_capabilities,
    add_graph,
    add_memory,
    git_init,
    make_memory_project,
    make_project,
    make_project_v2,
)

import projects_orchestrator.__main__ as cli
from projects_orchestrator import __version__
from projects_orchestrator.__main__ import main
from projects_orchestrator.adapters import cloud, forge


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    # `checks` now records history under XDG_STATE_HOME — isolate it too so no
    # test writes to the real ~/.local/state.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def test_version_is_set() -> None:
    assert __version__


def test_main_no_command_exits_zero() -> None:
    assert main([]) == 0


def test_version_flag_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


def test_projects_lists_discovered(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["projects", "--root", str(fleet_dir)])
    assert "alpha" in capsys.readouterr().out


def test_projects_json_is_parseable(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["projects", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["name"] == "alpha"


# --- With no --root, the fleet is PORT_ROOT (#313) ---
#
# Run from a directory holding no fleet.yaml, so what is under test is the
# default itself — the path a scheduled unit or a bare shell takes.


def _names(capsys) -> list[str]:
    return [row["name"] for row in json.loads(capsys.readouterr().out)]


def test_projects_defaults_to_an_exported_port_root(
    fleet_dir: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    make_project(fleet_dir, "alpha")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("PORT_ROOT", str(fleet_dir))
    main(["projects", "--json"])
    assert _names(capsys) == ["alpha"]


def test_projects_defaults_to_port_under_home_when_port_root_is_unset(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = tmp_path / "home"
    make_project(home / "port", "alpha")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("PORT_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(home))
    main(["projects", "--json"])
    assert _names(capsys) == ["alpha"]


def test_projects_root_flag_still_beats_port_root(
    fleet_dir: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    make_project(fleet_dir, "alpha")
    workspace = tmp_path / "workspace"
    make_project(workspace, "beta")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("PORT_ROOT", str(workspace))
    main(["projects", "--root", str(fleet_dir), "--json"])
    assert _names(capsys) == ["alpha"]


def test_status_renders_table(fleet_dir: Path, capsys) -> None:
    git_init(make_project(fleet_dir, "alpha"))
    main(["status", "--root", str(fleet_dir)])
    assert "clean" in capsys.readouterr().out


def test_status_single_project(fleet_dir: Path, capsys) -> None:
    git_init(make_project(fleet_dir, "alpha"))
    main(["status", "alpha", "--root", str(fleet_dir)])
    assert "alpha: clean on main" in capsys.readouterr().out


def _deployable(fleet_dir: Path, name: str = "alpha") -> Path:
    """A service project that actually ships the workflow a dispatch targets."""
    project = make_project_v2(fleet_dir, name, deploy_target="fly")
    workflow = project / ".github/workflows/deploy.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text("on: workflow_dispatch\n", encoding="utf-8")
    return project


def test_deploy_dry_run_is_planned(fleet_dir: Path, capsys) -> None:
    # Without --apply the deploy command dispatches nothing (ADR-005): it only
    # prints the plan, so it is safe to run from any surface.
    _deployable(fleet_dir)
    exit_code = main(["deploy", "alpha", "--root", str(fleet_dir)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "planned" in out


def test_deploy_dry_run_never_shells_out(fleet_dir: Path, monkeypatch) -> None:
    # The CLI is the ONE surface that can dispatch, so pin the dry-run guardrail
    # here too — on behaviour, not on the printed word "planned".
    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("dry run shelled out — the plan-only guardrail is broken")

    monkeypatch.setattr(cloud, "run_command", explode)
    _deployable(fleet_dir)
    assert main(["deploy", "alpha", "--root", str(fleet_dir)]) == 0


def test_deploy_non_service_is_skipped(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["deploy", "alpha", "--action", "rollback", "--root", str(fleet_dir)])
    assert "skipped" in capsys.readouterr().out


def test_deploy_apply_that_skips_exits_nonzero(fleet_dir: Path) -> None:
    # `deploy --apply && notify "rolled back"` must NOT announce a rollback that
    # never happened. Under an explicit --apply, skipped is a failure to act.
    make_project(fleet_dir, "alpha")
    assert (
        main(["deploy", "alpha", "--action", "rollback", "--apply", "--root", str(fleet_dir)]) == 1
    )


def test_deploy_apply_without_a_workflow_exits_nonzero(fleet_dir: Path) -> None:
    make_project_v2(fleet_dir, "alpha", deploy_target="fly")  # no deploy.yml
    assert main(["deploy", "alpha", "--apply", "--root", str(fleet_dir)]) == 1


def test_deploy_dry_run_that_skips_still_exits_zero(fleet_dir: Path) -> None:
    # Nothing was asked for, so nothing failing to happen is not an error.
    make_project(fleet_dir, "alpha")
    assert main(["deploy", "alpha", "--root", str(fleet_dir)]) == 0


def test_status_unknown_project_exits_2(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["status", "nope", "--root", str(fleet_dir)]) == 2


def test_checks_pass_exits_zero(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true", "test": "true"})
    assert main(["checks", "--root", str(fleet_dir)]) == 0


def test_checks_failure_exits_one(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "false"})
    assert main(["checks", "--root", str(fleet_dir)]) == 1


def test_checks_task_filter(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true", "test": "false"})
    main(["checks", "--root", str(fleet_dir), "--task", "lint"])
    assert "test" not in capsys.readouterr().out


def test_checks_updates_status_table(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    main(["checks", "--root", str(fleet_dir), "--task", "lint"])
    capsys.readouterr()
    main(["status", "--root", str(fleet_dir)])
    assert "pass" in capsys.readouterr().out


def test_memory_search_finds_fact(fleet_dir: Path, capsys) -> None:
    add_memory(make_project(fleet_dir, "alpha"), "project_context.md", body="uses postgres 16")
    main(["memory", "postgres", "--root", str(fleet_dir)])
    assert "postgres" in capsys.readouterr().out


def test_memory_search_json(fleet_dir: Path, capsys) -> None:
    add_memory(make_project(fleet_dir, "alpha"), "project_context.md", body="uses postgres 16")
    main(["memory", "postgres", "--root", str(fleet_dir), "--json"])
    hit = json.loads(capsys.readouterr().out)[0]
    assert hit["file"]["project"] == "alpha"
    assert isinstance(hit["score"], float) and hit["score"] > 0


def test_capabilities_summarizes_each_project(fleet_dir: Path, capsys) -> None:
    add_capabilities(make_project(fleet_dir, "alpha"), skills=["plan", "status"])
    main(["capabilities", "--root", str(fleet_dir)])
    assert "2 skill(s)" in capsys.readouterr().out


def test_capabilities_kind_inverts_to_projects(fleet_dir: Path, capsys) -> None:
    add_capabilities(make_project(fleet_dir, "alpha"), mcp_servers=["context7"])
    add_capabilities(make_project(fleet_dir, "beta"), mcp_servers=["context7"])
    main(["capabilities", "--root", str(fleet_dir), "--kind", "mcp"])
    assert "context7: alpha, beta" in capsys.readouterr().out


def test_capabilities_missing_inventory_is_reported(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["capabilities", "--root", str(fleet_dir)])
    assert "no CAPABILITIES.md" in capsys.readouterr().out


def test_capabilities_json_is_parseable(fleet_dir: Path, capsys) -> None:
    add_capabilities(make_project(fleet_dir, "alpha"), skills=["plan"])
    main(["capabilities", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["skills"][0]["name"] == "plan"


def test_memory_search_reads_graph_surface(fleet_dir: Path, capsys) -> None:
    project = make_memory_project(fleet_dir, "alpha", tier=2, graph_path="graphify-out/graph.json")
    add_graph(project, [{"name": "AuthService", "description": "handles oauth login"}])
    main(["memory", "oauth", "--root", str(fleet_dir)])
    assert "oauth" in capsys.readouterr().out


def test_memory_notes_unqueried_rag_endpoint(fleet_dir: Path, capsys) -> None:
    make_memory_project(
        fleet_dir,
        "alpha",
        tier=3,
        graph_path="graphify-out/graph.json",
        rag_endpoint="http://127.0.0.1:8099",
    )
    main(["memory", "anything", "--root", str(fleet_dir)])
    assert "RAG endpoint" in capsys.readouterr().err


def test_register_adds_scaffolded_project(tmp_path: Path, capsys) -> None:
    project = make_project(tmp_path, "alpha")
    result = tmp_path / "scaffold.json"
    result.write_text(
        json.dumps({"target": str(project), "contract_version": "1", "files_created": 42}),
        encoding="utf-8",
    )
    fleet_file = tmp_path / "fleet.yaml"
    assert main(["register", str(result), "--fleet", str(fleet_file)]) == 0
    assert "registered" in capsys.readouterr().out


def test_register_makes_project_discoverable(tmp_path: Path, capsys) -> None:
    project = make_project(tmp_path, "alpha")
    result = tmp_path / "scaffold.json"
    result.write_text(json.dumps({"target": str(project)}), encoding="utf-8")
    fleet_file = tmp_path / "fleet.yaml"
    main(["register", str(result), "--fleet", str(fleet_file)])
    capsys.readouterr()
    main(["projects", "--fleet", str(fleet_file)])
    assert "alpha" in capsys.readouterr().out


def test_register_invalid_result_exits_one(tmp_path: Path) -> None:
    result = tmp_path / "scaffold.json"
    result.write_text(json.dumps({"preset": "auto"}), encoding="utf-8")
    assert main(["register", str(result), "--fleet", str(tmp_path / "fleet.yaml")]) == 1


def test_register_missing_file_exits_two(tmp_path: Path) -> None:
    assert (
        main(["register", str(tmp_path / "absent.json"), "--fleet", str(tmp_path / "f.yaml")]) == 2
    )


def test_drift_no_manifest_exits_zero(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["drift", "--root", str(fleet_dir)]) == 0


def test_drift_json_reports_status(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["drift", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["status"] == "no-manifest"


def test_doctor_conformant_project_exits_zero(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["doctor", "--root", str(fleet_dir)]) == 0


def test_doctor_nonconformant_project_exits_one(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", config_text="project:\n  name: alpha\n")
    assert main(["doctor", "--root", str(fleet_dir)]) == 1


def test_doctor_json_reports_findings(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["doctor", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["findings"][0]["check"] == "config"


def test_audit_warn_exits_one(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["audit", "--root", str(fleet_dir)]) == 1


def test_audit_json_reports_findings(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["audit", "--root", str(fleet_dir), "--json"])
    categories = {f["category"] for f in json.loads(capsys.readouterr().out)[0]["findings"]}
    assert "freshness" in categories


def test_hardening_with_gaps_exits_one_and_groups_actions(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["hardening", "--root", str(fleet_dir)]) == 1
    out = capsys.readouterr().out
    assert "alpha:" in out
    assert "checks:" in out
    assert "memory:" in out


def test_hardening_json_is_parseable(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["hardening", "--root", str(fleet_dir), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["project"] == "alpha"
    assert {"checks", "memory"} <= {item["category"] for item in payload[0]["items"]}


def test_audit_markdown_renders_heading(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["audit", "--root", str(fleet_dir), "--markdown"])
    assert "## alpha" in capsys.readouterr().out


def test_audit_digest_first_run_exits_one_then_zero(fleet_dir: Path, tmp_path, monkeypatch) -> None:
    # First --digest run surfaces new issues (exit 1); an unchanged re-run has
    # no new issues (exit 0).
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    make_project(fleet_dir, "alpha")
    assert main(["audit", "--root", str(fleet_dir), "--digest"]) == 1
    assert main(["audit", "--root", str(fleet_dir), "--digest"]) == 0


def test_audit_digest_reports_no_change_on_second_run(
    fleet_dir: Path, tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    make_project(fleet_dir, "alpha")
    main(["audit", "--root", str(fleet_dir), "--digest"])
    capsys.readouterr()
    main(["audit", "--root", str(fleet_dir), "--digest"])
    assert "no change since last run" in capsys.readouterr().out


def test_audit_digest_webhook_posts_the_delta(fleet_dir: Path, monkeypatch) -> None:
    # The scheduled job's whole point: a changed digest reaches the sink (#98).
    posted: dict[str, object] = {}
    monkeypatch.setattr(cli, "post_payload", lambda url, payload: posted.update(url=url, **payload))
    make_project(fleet_dir, "alpha")
    main(["audit", "--root", str(fleet_dir), "--digest", "--webhook", "http://hook"])
    assert posted["url"] == "http://hook"
    assert posted["changed"] is True


def test_audit_digest_webhook_stays_silent_when_nothing_changed(
    fleet_dir: Path, monkeypatch
) -> None:
    # A daily cron must not post "no change" every day — only deltas.
    calls: list[str] = []
    monkeypatch.setattr(cli, "post_payload", lambda url, _payload: calls.append(url))
    make_project(fleet_dir, "alpha")
    main(["audit", "--root", str(fleet_dir), "--digest", "--webhook", "http://hook"])
    calls.clear()
    main(["audit", "--root", str(fleet_dir), "--digest", "--webhook", "http://hook"])
    assert calls == []


def test_audit_digest_survives_a_failing_webhook(fleet_dir: Path, monkeypatch) -> None:
    # Delivery is best-effort: a dead sink must not break the audit's exit code.
    monkeypatch.setattr(cli, "post_payload", lambda _url, _payload: False)
    make_project(fleet_dir, "alpha")
    assert main(["audit", "--root", str(fleet_dir), "--digest", "--webhook", "http://hook"]) == 1


def test_audit_webhook_without_digest_is_rejected(fleet_dir: Path) -> None:
    # --webhook only means something for a digest; a full report has no delta.
    make_project(fleet_dir, "alpha")
    assert main(["audit", "--root", str(fleet_dir), "--webhook", "http://hook"]) == 2


def test_ci_offline_exits_zero(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["ci", "--root", str(fleet_dir)]) == 0


def test_ci_json_reports_status(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["ci", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["ci"] == "unknown"


def test_ci_gitlab_host_reports_merge_requests(fleet_dir: Path, capsys) -> None:
    # A gitlab.com host routes through the glab adapter — the count unit is MR.
    config = (
        "project:\n  name: alpha\n  project_init_contract_version: 1\n"
        "  project_init_host: gitlab.com\nlanguage: python\n"
    )
    make_project(fleet_dir, "alpha", config_text=config)
    main(["ci", "--root", str(fleet_dir)])
    assert "open MR(s)" in capsys.readouterr().out


_CI_URL_CONFIG = (
    "project:\n  name: alpha\n  project_init_contract_version: 2\n"
    "  project_init_host: github.com\nlanguage: python\n"
    'ci:\n  status_url: "http://jenkins.example/job/alpha/lastBuild/api/json"\n'
)


def test_ci_declared_status_url_beats_the_forge(fleet_dir: Path, monkeypatch, capsys) -> None:
    # The project says "my CI is Jenkins" — even on a github.com host, gh must
    # not be the thing we ask, or we would report `unknown` forever (#100).
    # Patched on the forge router, which is where the routing now lives — the
    # `ci` CLI and the controller's /ci share it rather than each doing their own.
    monkeypatch.setattr(forge, "probe_status_url", lambda _d: "fail")
    monkeypatch.setattr(forge, "collect_github", _unreachable_forge)
    make_project(fleet_dir, "alpha", config_text=_CI_URL_CONFIG)
    assert main(["ci", "--root", str(fleet_dir)]) == 1
    assert "CI fail" in capsys.readouterr().out


def test_ci_without_a_status_url_still_uses_the_forge(fleet_dir: Path, capsys) -> None:
    # The default scaffold emits status_url: "" — behaviour must be unchanged.
    make_project(fleet_dir, "alpha")
    main(["ci", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["unit"] == "PR"


def _unreachable_forge(_descriptor):
    raise AssertionError("the forge adapter must not be probed when a status_url is declared")


def test_upgrade_plan_offline_renders(fleet_dir: Path, capsys, monkeypatch) -> None:
    make_project(fleet_dir, "alpha")
    monkeypatch.setattr(cli, "latest_upstream_version", lambda _cwd: None)
    monkeypatch.setattr(cli, "latest_plugin_version", lambda _cwd: None)
    main(["upgrade-plan", "--root", str(fleet_dir)])
    assert "alpha: unknown" in capsys.readouterr().out


def test_upgrade_plan_json_has_status(fleet_dir: Path, capsys, monkeypatch) -> None:
    make_project(fleet_dir, "alpha")
    monkeypatch.setattr(cli, "latest_upstream_version", lambda _cwd: None)
    monkeypatch.setattr(cli, "latest_plugin_version", lambda _cwd: None)
    main(["upgrade-plan", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["status"] == "unknown"


def test_upgrade_plan_renders_outdated_when_upstream_available(
    fleet_dir: Path, capsys, monkeypatch
) -> None:
    make_project(fleet_dir, "alpha")
    monkeypatch.setattr(cli, "latest_upstream_version", lambda _cwd: (0, 6, 0))
    monkeypatch.setattr(cli, "latest_plugin_version", lambda _cwd: None)
    main(["upgrade-plan", "--root", str(fleet_dir)])
    assert "alpha: outdated" in capsys.readouterr().out


def _plugin_project(fleet_dir: Path, name: str, plugin: str) -> None:
    make_project(
        fleet_dir,
        name,
        config_text=(
            "project:\n"
            f"  name: {name}\n"
            "  project_init_version: 1.2.2\n"
            f"  project_init_plugin_version: {plugin}\n"
        ),
    )


def test_upgrade_plan_reports_the_plugin_against_upstream_main(
    fleet_dir: Path, capsys, monkeypatch
) -> None:
    # #212: at upstream reads ok, behind reads behind, and neither changes the
    # scaffold status beside it.
    _plugin_project(fleet_dir, "alpha", "0.9.20")
    _plugin_project(fleet_dir, "beta", "0.9.16")
    monkeypatch.setattr(cli, "latest_upstream_version", lambda _cwd: (1, 2, 2))
    monkeypatch.setattr(cli, "latest_plugin_version", lambda _cwd: (0, 9, 20))
    assert main(["upgrade-plan", "--root", str(fleet_dir)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "alpha: ok (scaffold 1.2.2, drift -, PRs ?, plugin 0.9.20 ok)",
        "beta: ok (scaffold 1.2.2, drift -, PRs ?, plugin 0.9.16 → 0.9.20 behind)",
    ]


def test_upgrade_plan_plugin_is_unknown_never_ok_when_upstream_is_unreadable(
    fleet_dir: Path, capsys, monkeypatch
) -> None:
    _plugin_project(fleet_dir, "alpha", "0.9.20")
    monkeypatch.setattr(cli, "latest_upstream_version", lambda _cwd: (1, 2, 2))
    monkeypatch.setattr(cli, "latest_plugin_version", lambda _cwd: None)
    main(["upgrade-plan", "--root", str(fleet_dir), "--json"])
    row = json.loads(capsys.readouterr().out)[0]
    assert (row["plugin_version"], row["plugin_latest"], row["plugin_status"]) == (
        "0.9.20",
        "-",
        "unknown",
    )


def test_snapshot_json_has_descriptor(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["snapshot", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["descriptor"]["name"] == "alpha"


def test_snapshot_output_path_writes_html_without_html_flag(
    fleet_dir: Path, tmp_path: Path
) -> None:
    # -o implies --html: an operator naming a .html file must get the page,
    # not a silently-discarded text table.
    make_project(fleet_dir, "alpha")
    out = tmp_path / "fleet.html"
    assert main(["snapshot", "--root", str(fleet_dir), "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8").lstrip().startswith("<")


def test_start_no_run_command_exits_nonzero(fleet_dir: Path) -> None:
    # A project whose name contains "started" as a substring (e.g. "restarted")
    # must still report failure when it has no run_command.
    make_project(fleet_dir, "restarted")
    assert main(["start", "restarted", "--root", str(fleet_dir)]) == 1


def test_checks_records_history_read_back_by_history_command(
    fleet_dir: Path, tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    make_project(fleet_dir, "alpha", tooling={"test": "true"})
    main(["checks", "--root", str(fleet_dir), "--task", "test"])
    capsys.readouterr()
    assert main(["history", "alpha", "--root", str(fleet_dir)]) == 0
    out = capsys.readouterr().out
    assert "test:" in out and "+" in out


def test_history_unknown_project_exits_2(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["history", "ghost", "--root", str(fleet_dir)]) == 2


def test_history_no_runs_is_friendly(fleet_dir: Path, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    make_project(fleet_dir, "alpha")
    main(["history", "alpha", "--root", str(fleet_dir)])
    assert "no check history yet" in capsys.readouterr().out


def test_notify_clean_fleet_exits_zero(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha", tooling={"test": "true"})
    assert main(["notify", "--root", str(fleet_dir)]) == 0
    assert "no alerts" in capsys.readouterr().out


def test_notify_with_alerts_exits_one(fleet_dir: Path) -> None:
    # A project shipping git hooks that aren't installed trips a hooks alert.
    project = make_project(fleet_dir, "alpha")
    source = project / ".github" / "hooks"
    source.mkdir(parents=True)
    (source / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    assert main(["notify", "--root", str(fleet_dir)]) == 1


def test_watch_quiet_fleet_exits_zero(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true", "test": "true"})
    assert main(["watch", "--root", str(fleet_dir)]) == 0


def test_watch_failing_gate_exits_one(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"test": "false"})
    assert main(["watch", "--root", str(fleet_dir)]) == 1


def test_watch_renders_the_alert(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha", tooling={"test": "false"})
    main(["watch", "--root", str(fleet_dir)])
    assert "[critical] alpha: tests are failing" in capsys.readouterr().out


def test_watch_empty_root_exits_two(fleet_dir: Path) -> None:
    # A watch timer pointed at an empty root is a misconfiguration, not a
    # permanently quiet fleet — it must fail the unit, not report all-clear.
    assert main(["watch", "--root", str(fleet_dir)]) == 2


def test_watch_records_history_read_back_by_history_command(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha", tooling={"test": "true"})
    main(["watch", "--root", str(fleet_dir), "--task", "test"])
    capsys.readouterr()
    main(["history", "alpha", "--root", str(fleet_dir)])
    assert "test:" in capsys.readouterr().out


def test_watch_has_no_webhook_flag(fleet_dir: Path) -> None:
    # The box has one notifier (the scheduler that runs watch, which reads its exit code and
    # --json), so a scheduled watch can never be pointed at a webhook.
    with pytest.raises(SystemExit) as exc:
        main(["watch", "--root", str(fleet_dir), "--webhook", "https://hooks.example/x"])
    assert exc.value.code == 2


def test_watch_reports_alerts_but_never_pushes_them(fleet_dir: Path, monkeypatch, capsys) -> None:
    make_project(fleet_dir, "alpha", tooling={"test": "false"})
    posts: list[object] = []
    monkeypatch.setattr(cli, "post_webhook", lambda url, _alerts: posts.append(url) or True)
    monkeypatch.setattr(cli, "post_payload", lambda url, _payload: posts.append(url) or True)
    assert main(["watch", "--root", str(fleet_dir), "--task", "test", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["alerts"] and "webhook" not in payload
    assert posts == []


def test_watch_probes_remote_ci_before_alerting(fleet_dir: Path, monkeypatch) -> None:
    # Local gates green, but the forge turned red after the last manual `ci`
    # run: the scheduled watch must refresh remote state itself, not sleep on
    # a stale cache (PR #176 review).
    make_project(fleet_dir, "alpha", tooling={"lint": "true", "test": "true"})
    red = cli.CheckResult(
        project="alpha", task="ci", status="fail", checked_at="2026-07-17T00:00:00+00:00"
    )
    monkeypatch.setattr(
        cli,
        "probe_ci",
        lambda d: ({"project": d.name, "ci": "fail", "count": 0, "unit": "PR"}, [red], True),
    )
    assert main(["watch", "--root", str(fleet_dir)]) == 1


def test_watch_probes_cloud_state_before_alerting(fleet_dir: Path, monkeypatch, capsys) -> None:
    # Same for a deployment that went unhealthy since the last cloud-status run.
    make_project(fleet_dir, "alpha", tooling={"lint": "true", "test": "true"})
    sick = cli.CheckResult(
        project="alpha", task="cloud", status="fail", checked_at="2026-07-17T00:00:00+00:00"
    )
    monkeypatch.setattr(cli, "cloud_check_results", lambda _s, _at: [sick])
    main(["watch", "--root", str(fleet_dir)])
    assert "deployment is unhealthy" in capsys.readouterr().out


def _died_supervised(fleet_dir: Path) -> None:
    """A project whose supervised process has already died since last seen."""
    import subprocess

    from projects_orchestrator.descriptor import load_descriptor
    from projects_orchestrator.supervisor import start as sup_start

    project = make_project(
        fleet_dir, "alpha", tooling={"lint": "true", "test": "true", "run": "sleep 0.05"}
    )
    sup_start(load_descriptor(project))
    subprocess.run(["sleep", "0.3"], check=True)


def test_watch_alerts_on_a_dead_supervised_process(fleet_dir: Path, capsys) -> None:
    _died_supervised(fleet_dir)
    assert main(["watch", "--root", str(fleet_dir)]) == 1
    assert "process is down" in capsys.readouterr().out


def test_watch_retires_the_process_check_once_supervision_is_gone(fleet_dir: Path) -> None:
    # The death is observed once; the next pass reads the project as
    # unsupervised and must drop the stale fail rather than re-alert forever.
    _died_supervised(fleet_dir)
    main(["watch", "--root", str(fleet_dir)])
    assert main(["watch", "--root", str(fleet_dir)]) == 0


def test_watch_leaves_a_declared_process_gate_alone(fleet_dir: Path) -> None:
    # `process` can be a user-declared tooling gate; the liveness probe must
    # neither overwrite nor retire that gate's cached result (PR #177 review).
    from projects_orchestrator.cache import load_results

    make_project(fleet_dir, "alpha", tooling={"lint": "true", "process": "true"})
    main(["watch", "--root", str(fleet_dir), "--task", "process"])
    main(["watch", "--root", str(fleet_dir)])
    assert load_results()["alpha"]["process"].status == "pass"


def test_watch_json_carries_checks_and_alerts(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha", tooling={"test": "false"})
    main(["watch", "--root", str(fleet_dir), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"] and payload["alerts"]


def test_watch_changed_only_reuses_a_cached_pass(fleet_dir: Path, capsys) -> None:
    project = make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    git_init(project)
    main(["watch", "--root", str(fleet_dir), "--task", "lint", "--changed-only"])
    capsys.readouterr()
    main(["watch", "--root", str(fleet_dir), "--task", "lint", "--changed-only", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"][0]["cached"] is True


def test_serve_command_dispatches_to_server(fleet_dir: Path, monkeypatch) -> None:
    # serve() blocks, so verify wiring by capturing the call instead of running it.
    import projects_orchestrator.__main__ as cli

    captured = {}
    monkeypatch.setattr(
        cli,
        "serve",
        lambda _config, host, port, enable_actions: captured.update(
            host=host, port=port, enable_actions=enable_actions
        ),
    )
    assert main(["serve", "--root", str(fleet_dir), "--host", "0.0.0.0", "--port", "9999"]) == 0
    assert captured == {"host": "0.0.0.0", "port": 9999, "enable_actions": False}


def test_project_checks_drops_head_when_worktree_changes_mid_run(
    fleet_dir: Path, monkeypatch
) -> None:
    # If the worktree changes while gates run (then reverts), the stamped HEAD
    # must be cleared so --changed-only can never reuse the result.
    import projects_orchestrator.__main__ as cli
    from projects_orchestrator.descriptor import load_descriptor

    descriptor = load_descriptor(make_project(fleet_dir, "alpha", tooling={"lint": "true"}))
    heads = iter(["a" * 40, "b" * 40])  # before-run != after-run
    monkeypatch.setattr(cli, "clean_worktree_head", lambda _d: next(heads))
    (pair,) = cli._project_checks(descriptor, ("lint",), None, changed_only=False)
    result, _reused = pair
    assert result.head == ""


def test_fleet_file_drives_discovery(fleet_dir: Path, tmp_path: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text(f'roots: ["{fleet_dir}"]\n', encoding="utf-8")
    main(["projects", "--fleet", str(fleet_file)])
    assert "alpha" in capsys.readouterr().out


def test_events_no_logs_reports_none(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["events", "--root", str(fleet_dir)]) == 0
    assert "no events recorded" in capsys.readouterr().out


def test_events_reads_usage_log(fleet_dir: Path, capsys) -> None:
    project = make_project(fleet_dir, "alpha")
    log_dir = project / ".claude" / "observability"
    log_dir.mkdir(parents=True)
    (log_dir / "usage.jsonl").write_text(
        '{"ts": "2026-07-01T10:00:00+00:00", "hook": "prod_guard", "action": "block"}\n',
        encoding="utf-8",
    )
    main(["events", "--root", str(fleet_dir)])
    assert "[prod_guard] block" in capsys.readouterr().out


def test_events_since_filters(fleet_dir: Path, capsys) -> None:
    project = make_project(fleet_dir, "alpha")
    log_dir = project / ".claude" / "observability"
    log_dir.mkdir(parents=True)
    (log_dir / "usage.jsonl").write_text(
        '{"ts": "2026-07-01T10:00:00+00:00", "hook": "prod_guard", "action": "block"}\n',
        encoding="utf-8",
    )
    main(["events", "--root", str(fleet_dir), "--since", "2026-07-02T00:00:00+00:00"])
    assert "no events recorded" in capsys.readouterr().out


def test_events_json_is_parseable(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["events", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["project"] == "alpha"


def test_cloud_status_no_deploy_exits_zero(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["cloud-status", "--root", str(fleet_dir)]) == 0
    assert "alpha: none — none" in capsys.readouterr().out


def test_cloud_status_json_is_parseable(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["cloud-status", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["state"] == "none"


def test_cloud_status_caches_for_status_table(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["cloud-status", "--root", str(fleet_dir)])
    capsys.readouterr()
    main(["status", "--root", str(fleet_dir)])
    assert "none" in capsys.readouterr().out


def test_deploy_apply_json_failure_still_exits_nonzero(
    fleet_dir: Path, monkeypatch, capsys
) -> None:
    # --json must not launder a failure into a success. `_emit_json` returns 0,
    # so an early `return _emit_json(...)` would hand every JSON consumer the
    # exact success-that-wasn't the text path guards against:
    #   deploy alpha --apply --json && notify "rolled back"
    def failed(command: str, cwd, timeout: float = 0.0):  # noqa: ARG001 — run_command's signature
        from projects_orchestrator.runner import RunResult

        return RunResult(command=command, returncode=1, stdout="", stderr="gh: boom", duration=0.0)

    monkeypatch.setattr(cloud, "run_command", failed)
    _deployable(fleet_dir)
    exit_code = main(["deploy", "alpha", "--apply", "--json", "--root", str(fleet_dir)])
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert exit_code == 1


def test_deploy_apply_json_that_skips_exits_nonzero(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")  # no deploy target
    assert main(["deploy", "alpha", "--apply", "--json", "--root", str(fleet_dir)]) == 1


def test_deploy_dry_run_json_exits_zero(fleet_dir: Path) -> None:
    # A dry run asked for nothing, so it cannot have failed to do it.
    _deployable(fleet_dir)
    assert main(["deploy", "alpha", "--json", "--root", str(fleet_dir)]) == 0


# --- deploy --wait: poll-until-settled (#152) ---------------------------------


class _CLIDeploy:
    """Routes run_command by command: gh-run-list for watermark+polls, else dispatch.

    The first `gh run list` is the pre-dispatch watermark read; subsequent ones
    are polls. This lets a CLI test assert the ORDER (watermark before dispatch)
    as well as the settled outcome.
    """

    def __init__(self, run_lists: list[str], dispatch_ok: bool = True) -> None:
        from projects_orchestrator.runner import RunResult

        self._RunResult = RunResult
        self._run_lists = run_lists
        self._list_calls = 0
        self._dispatch_ok = dispatch_ok
        self.commands: list[str] = []

    def __call__(self, command: str, cwd, timeout: float = 0.0):  # noqa: ARG002
        self.commands.append(command)
        if command.startswith("gh run list"):
            idx = min(self._list_calls, len(self._run_lists) - 1)
            self._list_calls += 1
            return self._RunResult(command=command, returncode=0, stdout=self._run_lists[idx])
        return self._RunResult(command=command, returncode=0 if self._dispatch_ok else 1)


def _run_list(db_id: int, conclusion: str | None = "success", status: str = "completed") -> str:
    import json as _json

    return _json.dumps(
        [{"databaseId": db_id, "status": status, "conclusion": conclusion, "url": f"u/{db_id}"}]
    )


def test_deploy_wait_reports_success(fleet_dir: Path, monkeypatch, capsys) -> None:
    _deployable(fleet_dir)
    fake = _CLIDeploy([_run_list(41), _run_list(42, "success")])  # watermark 41, our run 42
    monkeypatch.setattr(cloud, "run_command", fake)
    code = main(["deploy", "alpha", "--apply", "--wait", "--root", str(fleet_dir)])
    assert code == 0
    assert "succeeded" in capsys.readouterr().out


def test_deploy_wait_on_a_failed_run_exits_nonzero(fleet_dir: Path, monkeypatch, capsys) -> None:
    # The whole point: we WATCHED it, so a failed deploy is a nonzero exit — not
    # the exit 0 a bare dispatch would have given for "successfully queued".
    _deployable(fleet_dir)
    fake = _CLIDeploy([_run_list(41), _run_list(42, "failure")])
    monkeypatch.setattr(cloud, "run_command", fake)
    code = main(["deploy", "alpha", "--apply", "--wait", "--root", str(fleet_dir)])
    assert code == 1
    assert "failed" in capsys.readouterr().out


def test_deploy_wait_reads_the_watermark_before_dispatching(fleet_dir: Path, monkeypatch) -> None:
    # Ordering is load-bearing: the watermark must be captured before the dispatch
    # creates our run, or our own run sets the floor above itself.
    _deployable(fleet_dir)
    fake = _CLIDeploy([_run_list(41), _run_list(42, "success")])
    monkeypatch.setattr(cloud, "run_command", fake)
    main(["deploy", "alpha", "--apply", "--wait", "--root", str(fleet_dir)])
    assert fake.commands[0].startswith("gh run list")  # watermark
    assert fake.commands[1].startswith("gh workflow run")  # dispatch, AFTER


def test_deploy_wait_without_apply_does_not_poll(fleet_dir: Path, monkeypatch, capsys) -> None:
    # --wait is meaningless on a dry run: nothing was dispatched to follow.
    _deployable(fleet_dir)
    fake = _CLIDeploy([_run_list(41)])
    monkeypatch.setattr(cloud, "run_command", fake)
    code = main(["deploy", "alpha", "--wait", "--root", str(fleet_dir)])
    assert code == 0
    assert "planned" in capsys.readouterr().out
    assert fake.commands == []  # never shelled out at all


def test_deploy_wait_json_carries_the_settlement(fleet_dir: Path, monkeypatch, capsys) -> None:
    _deployable(fleet_dir)
    fake = _CLIDeploy([_run_list(41), _run_list(42, "success")])
    monkeypatch.setattr(cloud, "run_command", fake)
    main(["deploy", "alpha", "--apply", "--wait", "--json", "--root", str(fleet_dir)])
    import json as _json

    payload = _json.loads(capsys.readouterr().out)
    assert payload["settlement"]["state"] == "succeeded"


def test_deploy_wait_on_gitlab_rejects_before_dispatching(
    fleet_dir: Path, monkeypatch, capsys
) -> None:
    # The wait is unsupported for glab, so it must be refused BEFORE the dispatch
    # fires — otherwise a retry of the nonzero exit double-dispatches the pipeline.
    config = (
        "project:\n  name: alpha\n  project_init_contract_version: 2\n"
        "  project_init_host: gitlab.com\n"
        "language: python\ndelivery: service\n"
        "deploy:\n  target: fly\n  app: alpha-svc\n"
    )
    project = make_project(fleet_dir, "alpha", config_text=config)
    (project / ".gitlab").mkdir(parents=True, exist_ok=True)
    (project / ".gitlab/deploy.yml").write_text("x\n", encoding="utf-8")

    def explode(*_a: object, **_k: object) -> object:
        raise AssertionError("--wait on gitlab shelled out — it must be rejected first")

    monkeypatch.setattr(cloud, "run_command", explode)
    code = main(["deploy", "alpha", "--apply", "--wait", "--root", str(fleet_dir)])
    assert code == 2
    assert "not supported for GitLab" in capsys.readouterr().err


def test_serve_enable_actions_rejects_non_loopback_host(capsys) -> None:
    # The guard returns before the blocking serve loop, so this is safe to call.
    assert main(["serve", "--enable-actions", "--host", "0.0.0.0"]) == 2
    assert "loopback" in capsys.readouterr().err


@pytest.fixture(autouse=True)
def _no_live_heal_agent(monkeypatch) -> None:
    """No CLI test may spawn a real ``claude`` via heal — fuse it shut.

    heal's CLI reaches ``_default_agent_run`` whenever a gate is already red. A
    test that means to exercise a heal injects its own fake, overriding this.
    """

    def _explode(*_args: object, **_kwargs: object) -> object:
        message = "a CLI test reached the REAL heal agent — inject a fake"
        raise AssertionError(message)

    monkeypatch.setattr("projects_orchestrator.heal._default_agent_run", _explode)


def test_heal_requires_a_target(capsys) -> None:
    assert main(["heal"]) == 2
    assert "name one project" in capsys.readouterr().err


def test_heal_rejects_project_and_all_together(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["heal", "alpha", "--all", "--root", str(fleet_dir)]) == 2


def test_heal_rejects_limit_below_one(capsys) -> None:
    assert main(["heal", "--all", "--limit", "0"]) == 2
    assert "--limit must be at least 1" in capsys.readouterr().err


def test_heal_unknown_project_exits_two(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["heal", "ghost", "--root", str(fleet_dir)]) == 2
    assert "unknown project: ghost" in capsys.readouterr().err


def test_heal_all_notify_mode_reports_without_spawning(fleet_dir: Path, capsys) -> None:
    # The _no_live_heal_agent fuse proves no agent is reached: a red gate under
    # --mode notify is diagnosed, not fixed, and the pass is still eventful.
    make_project(fleet_dir, "alpha", tooling={"lint": "false"})
    assert main(["heal", "--all", "--mode", "notify", "--root", str(fleet_dir)]) == 1
    assert "notified" in capsys.readouterr().out


def test_heal_all_is_quiet_and_exits_zero_when_fleet_is_green(fleet_dir: Path, capsys) -> None:
    # lint passes for both -> nothing failing -> no agent is ever reached, exit 0.
    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    make_project(fleet_dir, "beta", tooling={"lint": "true"})
    assert main(["heal", "--all", "--root", str(fleet_dir)]) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_heal_json_reports_eventful_false_for_a_green_project(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    assert main(["heal", "alpha", "--root", str(fleet_dir), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["eventful"] is False
    assert payload["attempted"] == []


def test_heal_single_project_exits_one_when_eventful(fleet_dir: Path, monkeypatch, capsys) -> None:
    from projects_orchestrator.heal import AgentOutcome

    project = make_project(fleet_dir, "alpha", tooling={"lint": "test -f fixed.txt"})
    git_init(project)

    def useless_agent(_descriptor: object, _prompt: str) -> AgentOutcome:
        return AgentOutcome(ok=True, summary="looked, changed nothing")

    monkeypatch.setattr("projects_orchestrator.heal._default_agent_run", useless_agent)
    # lint is red (no fixed.txt); the fake agent changes nothing, so re-verify
    # still fails -> VERIFY_FAILED -> an eventful pass -> exit 1.
    assert main(["heal", "alpha", "--root", str(fleet_dir)]) == 1
    assert "verify_failed" in capsys.readouterr().out


# --- An unresolved fleet is not a clean bill of health (#204) ---
#
# The reporting commands used to report an empty fleet and exit 0, and
# `doctor`/`drift`/`audit` printed nothing at all — byte-identical to a fully
# conformant fleet. `doctor` was the worst of them, because it is the command
# you run TO FIND OUT whether the fleet is set up.

_REPORTING_COMMANDS = ["projects", "status", "doctor", "drift", "audit", "hardening"]


@pytest.mark.parametrize("command", _REPORTING_COMMANDS)
def test_a_reporting_command_fails_on_an_unresolved_fleet(command: str, tmp_path: Path) -> None:
    empty = tmp_path / "nothing-here"
    empty.mkdir()
    assert main([command, "--root", str(empty)]) == 2


@pytest.mark.parametrize("command", _REPORTING_COMMANDS)
def test_a_reporting_command_still_fails_on_an_unresolved_fleet_with_json(
    command: str, tmp_path: Path
) -> None:
    """`--json` was the worst arm: a consumer reading the document saw a
    well-formed empty result and a success code."""
    empty = tmp_path / "nothing-here"
    empty.mkdir()
    assert main([command, "--root", str(empty), "--json"]) == 2


def test_a_resolved_fleet_still_exits_zero(fleet_dir: Path) -> None:
    """The discrimination that matters: this fires on an EMPTY fleet, never on
    a populated one."""
    make_project(fleet_dir, "alpha")
    assert main(["projects", "--root", str(fleet_dir)]) == 0


def test_a_command_keeps_its_own_failure_code_on_a_resolved_fleet(fleet_dir: Path) -> None:
    """The `ok` passthrough: a command that already fails for its own reasons
    must keep that code, not have it overwritten by the empty-fleet code."""
    make_project(fleet_dir, "alpha")
    assert main(["hardening", "--root", str(fleet_dir)]) == 1


def test_register_still_works_on_an_empty_fleet(tmp_path: Path) -> None:
    """THE REGRESSION THIS FIX HAD TO AVOID, and the reason `_discover` refused
    to carry the check: an empty fleet is the NORMAL first-run state for
    `register`, and failing there would break the first registration on a new
    machine. The check is opted into per reporting command for exactly this."""
    project = make_project(tmp_path / "elsewhere", "gamma")
    result = tmp_path / "scaffold.json"
    result.write_text(
        json.dumps({"target": str(project), "contract_version": "1", "files_created": 1}),
        encoding="utf-8",
    )
    fleet_file = tmp_path / "fleet.yaml"
    assert not fleet_file.exists(), "the first-run state: no fleet file yet"
    assert main(["register", str(result), "--fleet", str(fleet_file)]) == 0


def test_audit_digest_does_not_persist_a_baseline_on_an_unresolved_fleet(
    fleet_dir: Path, tmp_path: Path
) -> None:
    """Raised in review on #239, and it is the sharper half of #204.

    `_emit_digest` PERSISTS a new baseline and can post a webhook, and Python
    evaluates an argument before the call it is passed to — so wrapping it in
    the empty-fleet check ran the digest first. On an unresolved fleet that
    marks every prior finding as resolved, writes the empty baseline, and with
    `--webhook` publishes the false resolution, returning 2 only afterwards. A
    transient discovery failure would corrupt every later delta.
    """
    from projects_orchestrator.digest import digest_path

    make_project(fleet_dir, "alpha")
    assert main(["audit", "--root", str(fleet_dir), "--digest"]) == 1
    baseline = digest_path().read_text(encoding="utf-8")
    assert "alpha" in baseline, "precondition: prior findings are recorded"

    empty = tmp_path / "nothing-here"
    empty.mkdir()
    assert main(["audit", "--root", str(empty), "--digest"]) == 2
    assert digest_path().read_text(encoding="utf-8") == baseline


def test_status_warns_when_watch_has_never_run(fleet_dir: Path, capsys) -> None:
    """#186: `status` is where a human looks, and the watch pass cannot report
    its own death. A box whose timer was never installed must not read green."""
    make_project(fleet_dir, "alpha")
    main(["status", "--root", str(fleet_dir)])
    assert "watch has never run" in capsys.readouterr().err


def test_status_is_quiet_when_watch_is_fresh(fleet_dir: Path, capsys) -> None:
    """Only a fault speaks. An advisory on every ordinary `status` is the noise
    that trains people to skim past the one that mattered."""
    from projects_orchestrator import watchdog

    make_project(fleet_dir, "alpha")
    watchdog.record_pass(3600)
    main(["status", "--root", str(fleet_dir)])
    assert "watch" not in capsys.readouterr().err


def test_a_watch_pass_records_its_own_heartbeat(fleet_dir: Path) -> None:
    """History only carries recordable results, so a pass that produced none
    left no trace and was indistinguishable from a pass that never happened."""
    from projects_orchestrator import watchdog

    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    main(["watch", "--root", str(fleet_dir), "--interval", "3600"])
    assert watchdog.read_state().status == watchdog.FRESH


def test_status_for_one_project_also_warns_about_watch(fleet_dir: Path, capsys) -> None:
    """Raised in review on #244: `status PROJECT` returned before the heartbeat
    was read, so a human drilling into one project was told nothing."""
    make_project(fleet_dir, "alpha")
    main(["status", "alpha", "--root", str(fleet_dir)])
    assert "watch has never run" in capsys.readouterr().err


def test_status_json_also_warns_about_watch(fleet_dir: Path, capsys) -> None:
    """The warning goes to stderr so the JSON document on stdout stays exactly
    the array its consumers parse."""
    make_project(fleet_dir, "alpha")
    main(["status", "--root", str(fleet_dir), "--json"])
    captured = capsys.readouterr()
    assert "watch has never run" in captured.err
    assert json.loads(captured.out) == [] or isinstance(json.loads(captured.out), list)


@pytest.mark.parametrize(
    "system", ["MINGW64_NT-10.0-22631", "MSYS_NT-10.0", "CYGWIN_NT-10.0", "Windows"]
)
def test_native_windows_is_refused_naming_wsl2(system: str, monkeypatch, capsys) -> None:
    """A native Windows shell exits non-zero before parsing, naming WSL2 (#326)."""
    monkeypatch.setattr("platform.system", lambda: system)
    assert main(["--version"]) != 0
    assert "WSL2" in capsys.readouterr().err


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_linux_and_darwin_are_not_refused(system: str, monkeypatch) -> None:
    """Supported platforms reach the normal command path."""
    monkeypatch.setattr("platform.system", lambda: system)
    assert main([]) == 0

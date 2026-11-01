"""Descriptor parsing: contract v1 in, dataclass out, never raise."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_project, make_project_v2

from projects_orchestrator.capabilities import load_capabilities
from projects_orchestrator.descriptor import (
    MEMORY_STACK_NONE,
    contract_label,
    load_descriptor,
    parse_config,
    parse_scaffold_version,
    resolve_config,
)
from projects_orchestrator.observability import observability_dir


def test_parse_scaffold_version_reads_dotted_integers() -> None:
    assert parse_scaffold_version("0.5.2") == (0, 5, 2)


def test_parse_scaffold_version_unknown_is_none() -> None:
    assert parse_scaffold_version("unknown") is None


def test_parse_scaffold_version_non_numeric_is_none() -> None:
    assert parse_scaffold_version("1.2.beta") is None


def test_parse_scaffold_version_two_components_is_none() -> None:
    assert parse_scaffold_version("0.6") is None


def test_parse_scaffold_version_bare_integer_is_none() -> None:
    assert parse_scaffold_version("999") is None


def test_parse_scaffold_version_orders_by_component() -> None:
    assert parse_scaffold_version("0.6.0") > parse_scaffold_version("0.5.9")


def test_load_descriptor_reads_name(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    assert load_descriptor(project).name == "alpha"


def test_load_descriptor_reads_tooling_commands(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", tooling={"lint": "true", "test": "false"})
    assert load_descriptor(project).tooling == {"lint": "true", "test": "false"}


def test_load_descriptor_reads_host(tmp_path: Path) -> None:
    text = "project:\n  name: alpha\n  project_init_host: gitlab.com\n"
    assert parse_config(text, tmp_path).host == "gitlab.com"


def test_load_descriptor_host_defaults_empty(tmp_path: Path) -> None:
    assert parse_config("project:\n  name: alpha\n", tmp_path).host == ""


def test_load_descriptor_reads_contract_version(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    assert load_descriptor(project).contract_version == 1


def test_load_descriptor_resolves_memory_path(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    assert load_descriptor(project).memory_path == project.resolve() / ".claude/memory"


def test_load_descriptor_returns_none_for_non_project(tmp_path: Path) -> None:
    assert load_descriptor(tmp_path) is None


def test_load_descriptor_non_utf8_config_does_not_raise(tmp_path: Path) -> None:
    # A config saved in a non-UTF-8 encoding must degrade, not crash discovery.
    project = tmp_path / "cafe"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "config.yaml").write_bytes(b"project:\n  name: caf\xe9\n")
    descriptor = load_descriptor(project)
    assert descriptor is not None


def test_parse_config_invalid_yaml_degrades_with_warning(tmp_path: Path) -> None:
    descriptor = parse_config("{unclosed: [", tmp_path)
    assert descriptor.warnings != ()


def test_parse_config_invalid_yaml_falls_back_to_dir_name(tmp_path: Path) -> None:
    descriptor = parse_config("{unclosed: [", tmp_path / "beta")
    assert descriptor.name == "beta"


def test_parse_config_empty_file_warns(tmp_path: Path) -> None:
    assert parse_config("", tmp_path).warnings == ("config.yaml is empty",)


def test_parse_config_ignores_blank_tooling_commands(tmp_path: Path) -> None:
    descriptor = parse_config('tooling:\n  lint_command: "  "\n', tmp_path)
    assert descriptor.has_task("lint") is False


def test_has_task_true_for_declared_command(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", tooling={"run": "echo hi"})
    assert load_descriptor(project).has_task("run") is True


def test_parse_config_tolerates_non_string_tooling_key(tmp_path: Path) -> None:
    # PyYAML coerces a bare `on:` key to the bool True; must not crash.
    descriptor = parse_config('tooling:\n  on: echo hi\n  lint_command: "ruff check"\n', tmp_path)
    assert descriptor.has_task("lint") is True


def test_v2_config_parses_deploy_block(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project_v2(fleet_dir, "alpha", deploy_target="fly"))
    assert descriptor.deploy.target == "fly"


def test_v2_config_parses_health_url(fleet_dir: Path) -> None:
    project = make_project_v2(fleet_dir, "alpha", health_url="https://x.example/health")
    assert load_descriptor(project).deploy.health_url == "https://x.example/health"


def test_v2_config_parses_deploy_workflow(tmp_path: Path) -> None:
    text = (
        "project:\n  project_init_contract_version: 2\n"
        "deploy:\n  target: fly\n  workflow: ship.yml\n"
    )
    assert parse_config(text, tmp_path).deploy.workflow == "ship.yml"


def test_v2_deploy_workflow_defaults_empty(tmp_path: Path) -> None:
    text = "project:\n  project_init_contract_version: 2\ndeploy:\n  target: fly\n"
    assert parse_config(text, tmp_path).deploy.workflow == ""


def test_v2_config_resolves_observability_path(fleet_dir: Path) -> None:
    project = make_project_v2(fleet_dir, "alpha", observability_path="logs/agent")
    assert load_descriptor(project).observability_path == project.resolve() / "logs/agent"


def test_v2_config_parses_hooks_expected(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project_v2(fleet_dir, "alpha"))
    assert descriptor.hooks_expected == ("pre-commit", "commit-msg")


def test_memory_path_escaping_project_root_is_clamped(tmp_path: Path) -> None:
    text = "project:\n  name: alpha\nmemory:\n  memory_path: ../../etc\n"
    descriptor = parse_config(text, tmp_path)
    assert descriptor.memory_path == tmp_path.resolve() / ".claude/memory"


def test_memory_path_escaping_project_root_warns(tmp_path: Path) -> None:
    text = "project:\n  name: alpha\nmemory:\n  memory_path: /etc\n"
    assert any("escapes the project root" in w for w in parse_config(text, tmp_path).warnings)


def test_observability_path_escaping_project_root_is_ignored(tmp_path: Path) -> None:
    text = (
        "project:\n  name: alpha\n  project_init_contract_version: 2\n"
        "observability:\n  path: ../../var/log\n"
    )
    descriptor = parse_config(text, tmp_path)
    assert descriptor.observability_path is None
    assert any("escapes the project root" in w for w in descriptor.warnings)


def test_v1_config_ignores_v2_fields(tmp_path: Path) -> None:
    text = "project:\n  name: alpha\n  project_init_contract_version: 1\ndeploy:\n  target: fly\n"
    assert parse_config(text, tmp_path).deploy is None


def test_v1_config_has_no_hooks_expected(tmp_path: Path) -> None:
    text = (
        "project:\n  name: alpha\n  project_init_contract_version: 1\n"
        "hooks:\n  expected: [pre-commit]\n"
    )
    assert parse_config(text, tmp_path).hooks_expected == ()


def test_v2_config_without_deploy_block_is_none(tmp_path: Path) -> None:
    text = "project:\n  name: alpha\n  project_init_contract_version: 2\n"
    assert parse_config(text, tmp_path).deploy is None


def test_v2_deploy_defaults_to_none_target(tmp_path: Path) -> None:
    text = "project:\n  name: alpha\n  project_init_contract_version: 2\ndeploy:\n  app: svc\n"
    assert parse_config(text, tmp_path).deploy.target == "none"


_LADDER = ("auto", "obsidian-only", "obsidian-graphify", "obsidian-graphify-rag")


def _memory_config(tier: int | str, extra: str = "", stack: str = "") -> str:
    # The stack defaults to the one that carries `tier`: a tier the stack
    # contradicts is its own case, tested below (#257).
    return (
        "project:\n  name: alpha\n"
        f"memory:\n  tier: {tier}\n  stack: {stack or _LADDER[int(tier)]}\n"
        "  memory_path: .claude/memory\n" + extra
    )


def test_descriptor_reads_memory_stack(tmp_path: Path) -> None:
    assert parse_config(_memory_config(3), tmp_path).memory_stack == "obsidian-graphify-rag"


def test_memory_stack_defaults_unknown(tmp_path: Path) -> None:
    assert parse_config("project:\n  name: alpha\n", tmp_path).memory_stack == "unknown"


def test_tier1_config_exposes_vault_path(tmp_path: Path) -> None:
    descriptor = parse_config(_memory_config(1, "  vault_path: .claude/vault\n"), tmp_path)
    assert descriptor.vault_path == tmp_path.resolve() / ".claude/vault"


def test_tier0_config_ignores_vault_path(tmp_path: Path) -> None:
    # Below tier 1 the vault surface does not exist — a stray value is ignored.
    descriptor = parse_config(_memory_config(0, "  vault_path: .claude/vault\n"), tmp_path)
    assert descriptor.vault_path is None


def test_tier2_config_exposes_graph_path(tmp_path: Path) -> None:
    descriptor = parse_config(
        _memory_config(2, "  graph_path: graphify-out/graph.json\n"), tmp_path
    )
    assert descriptor.graph_path == tmp_path.resolve() / "graphify-out/graph.json"


def test_tier1_config_ignores_graph_path(tmp_path: Path) -> None:
    descriptor = parse_config(
        _memory_config(1, "  graph_path: graphify-out/graph.json\n"), tmp_path
    )
    assert descriptor.graph_path is None


def test_tier3_config_exposes_rag_endpoint(tmp_path: Path) -> None:
    descriptor = parse_config(
        _memory_config(3, "  rag_endpoint: http://127.0.0.1:8099\n"), tmp_path
    )
    assert descriptor.rag_endpoint == "http://127.0.0.1:8099"


def test_tier2_config_ignores_rag_endpoint(tmp_path: Path) -> None:
    descriptor = parse_config(
        _memory_config(2, "  rag_endpoint: http://127.0.0.1:8099\n"), tmp_path
    )
    assert descriptor.rag_endpoint == ""


def test_tier3_unset_rag_endpoint_is_empty(tmp_path: Path) -> None:
    # A tier-3 child that has not run its RAG setup yet omits the endpoint.
    assert parse_config(_memory_config(3), tmp_path).rag_endpoint == ""


def test_vault_path_escaping_project_root_is_ignored(tmp_path: Path) -> None:
    descriptor = parse_config(_memory_config(1, "  vault_path: ../../etc\n"), tmp_path)
    assert descriptor.vault_path is None


def test_vault_path_escaping_project_root_warns(tmp_path: Path) -> None:
    descriptor = parse_config(_memory_config(1, "  vault_path: /etc\n"), tmp_path)
    assert any("escapes the project root" in w for w in descriptor.warnings)


# --- #236: a surface declared below its gate is dropped, and SAYS so ----------

_GATED = (
    ("vault_path", "  vault_path: .claude/vault\n", 1),
    ("graph_path", "  graph_path: graphify-out/graph.json\n", 2),
    ("rag_endpoint", "  rag_endpoint: http://127.0.0.1:8099\n", 3),
)


@pytest.mark.parametrize(("key", "line", "gate"), _GATED)
def test_a_surface_declared_one_rung_below_its_gate_warns(
    tmp_path: Path, key: str, line: str, gate: int
) -> None:
    descriptor = parse_config(_memory_config(gate - 1, line), tmp_path)
    assert descriptor.warnings == (
        f"memory.{key} is declared at memory tier {gate - 1} but needs tier {gate}+ — ignored",
    )


def test_an_escaping_path_below_its_gate_warns_as_an_escape(tmp_path: Path) -> None:
    # Gating first let this through both checks without a word.
    descriptor = parse_config(_memory_config(1, "  graph_path: ../../etc/graph.json\n"), tmp_path)
    assert descriptor.graph_path is None
    assert descriptor.warnings == (
        "memory.graph_path '../../etc/graph.json' escapes the project root — ignored",
    )


def test_a_symlink_loop_below_the_gate_warns_instead_of_raising(tmp_path: Path) -> None:
    # Containment now runs before the gate, so a below-gate path is resolved,
    # and on Python 3.11/3.12 resolving a symlink loop raises RuntimeError. There
    # it is rejected as uncontainable; 3.13+ resolves it and it is below the gate.
    # Either way: one warning, no value, no exception.
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    descriptor = parse_config(_memory_config(0, "  vault_path: loop/vault\n"), tmp_path)
    assert descriptor.vault_path is None
    assert len(descriptor.warnings) == 1
    assert descriptor.warnings[0].startswith("memory.vault_path ")


@pytest.mark.parametrize("escape", ["\\0", "\\uD800"], ids=["nul", "lone-surrogate"])
@pytest.mark.parametrize("line", ['  vault_path: "bad{}path"\n', '  memory_path: "bad{}path"\n'])
def test_an_unresolvable_path_warns_instead_of_raising(
    tmp_path: Path, escape: str, line: str
) -> None:
    # Valid YAML escapes that decode to a NUL or a lone surrogate make
    # Path.resolve() raise ValueError / UnicodeEncodeError. `memory_path` is
    # resolved at every tier, so it could already take discovery down; the
    # below-gate `vault_path` became reachable with this change.
    descriptor = parse_config(_memory_config(0, line.format(escape)), tmp_path)
    assert descriptor.vault_path is None
    assert len(descriptor.warnings) == 1
    assert "cannot be resolved" in descriptor.warnings[0]
    # The warning quotes the value, and doctor/audit print it: it must encode.
    descriptor.warnings[0].encode("utf-8")
    assert escape.lower() in descriptor.warnings[0].replace("\\x00", "\\0").lower()


def test_a_warning_keeps_printable_non_ascii_as_written(tmp_path: Path) -> None:
    # The control: escaping must not mangle an ordinary non-ASCII path.
    descriptor = parse_config(_memory_config(1, "  vault_path: ../užrašai\n"), tmp_path)
    assert descriptor.warnings == (
        "memory.vault_path '../užrašai' escapes the project root — ignored",
    )


def test_every_surface_at_its_own_tier_is_silent(tmp_path: Path) -> None:
    # The control: a reader that warned on every declared surface would pass the
    # two tests above. Each rung declares exactly what the template renders for
    # it, including tier 3's empty `rag_endpoint:` before RAG setup has run.
    rungs = {
        0: "",
        1: "  vault_path: .claude/vault\n",
        2: "  vault_path: .claude/vault\n  graph_path: graphify-out/graph.json\n",
        3: "  vault_path: .claude/vault\n  graph_path: graphify-out/graph.json\n  rag_endpoint:\n",
    }
    for tier, extra in rungs.items():
        assert parse_config(_memory_config(tier, extra), tmp_path).warnings == (), tier


class TestScaffoldLayout:
    """PI-627: the descriptor lives under ``.agents/`` (current project-init)
    or ``.claude/`` (legacy). The reader must find both — a current scaffold
    has no ``.claude/config.yaml`` at all, so hardcoding it dropped every
    modern project from discovery."""

    def test_discovers_agents_layout(self, fleet_dir: Path) -> None:
        project = make_project(fleet_dir, "modern", layout=".agents")
        d = load_descriptor(project)
        assert d is not None
        assert d.config_root == ".agents"
        assert d.config_path == project / ".agents" / "config.yaml"

    def test_legacy_claude_layout_still_works(self, fleet_dir: Path) -> None:
        project = make_project(fleet_dir, "legacy", layout=".claude")
        d = load_descriptor(project)
        assert d is not None
        assert d.config_root == ".claude"

    def test_agents_preferred_when_both_present(self, fleet_dir: Path) -> None:
        # A stray legacy .claude/config.yaml must not shadow the current one.
        project = make_project(fleet_dir, "both", layout=".agents")
        (project / ".claude").mkdir(parents=True, exist_ok=True)
        (project / ".claude" / "config.yaml").write_text(
            'project:\n  name: "stale"\n', encoding="utf-8"
        )
        d = load_descriptor(project)
        assert d is not None
        assert d.config_root == ".agents"
        assert d.name == "both"

    def test_no_config_is_not_a_project(self, tmp_path: Path) -> None:
        assert load_descriptor(tmp_path) is None
        assert resolve_config(tmp_path) is None

    def test_memory_defaults_under_config_root(self, tmp_path: Path) -> None:
        # A config that omits memory_path anchors it beside the descriptor.
        d = parse_config('project:\n  name: "m"\n', tmp_path, config_root=".agents")
        assert d.memory_path == tmp_path / ".agents" / "memory"

    def test_capabilities_read_under_agents_layout(self, fleet_dir: Path) -> None:
        project = make_project(fleet_dir, "modern", layout=".agents")
        (project / ".agents" / "CAPABILITIES.md").write_text(
            "# Capabilities\n\n## Skills (1)\n\n| Skill | Description |\n|---|---|\n| plan | plan |\n",
            encoding="utf-8",
        )
        d = load_descriptor(project)
        assert d is not None
        cap = load_capabilities(d)
        assert cap.present

    def test_observability_dir_defaults_under_config_root(self, tmp_path: Path) -> None:
        d = parse_config('project:\n  name: "o"\n', tmp_path, config_root=".agents")
        assert observability_dir(d) == tmp_path / ".agents" / "observability"


def test_declared_heal_mode_notify_is_parsed(tmp_path: Path) -> None:
    project = make_project(
        tmp_path, "alpha", config_text="project:\n  name: alpha\nheal:\n  mode: notify\n"
    )
    descriptor = load_descriptor(project)
    assert descriptor is not None and descriptor.heal_mode == "notify"


def test_heal_mode_defaults_to_empty_when_undeclared(tmp_path: Path) -> None:
    descriptor = load_descriptor(make_project(tmp_path, "alpha"))
    assert descriptor is not None and descriptor.heal_mode == ""


def test_unknown_heal_mode_is_ignored_with_a_warning(tmp_path: Path) -> None:
    # A typo'd mode must not silently switch a project between "spends money
    # and opens PRs" and "tells me and stops" — ignore it, loudly.
    project = make_project(
        tmp_path, "alpha", config_text="project:\n  name: alpha\nheal:\n  mode: yolo\n"
    )
    descriptor = load_descriptor(project)
    assert descriptor is not None
    assert descriptor.heal_mode == "" and any("heal.mode" in w for w in descriptor.warnings)


class TestBoundaryMarkerValue:
    """Marker contract case H1 — the detect-and-defer ``context:`` declaration.

    project-init has emitted this key since PI-901 and the ambient layer has read
    it since 2026-07-25; the fleet read it nowhere, so a value written by one
    layer and honoured by another was invisible to the layer that reports on
    both. Case ids are the marker contract's shared fixtures (``cases.json``)
    so a divergence is traceable to one line of one contract.

    The fleet does not ACT on the value — an opted-out project is still a
    project it manages — it surfaces it. Dropping such a project from discovery
    would delete it from ``projects``, ``snapshot``, ``audit`` and ``heal`` on
    the strength of a field about which *agent layer* governs.
    """

    def test_context_repo_is_read(self, tmp_path: Path) -> None:
        """M20: what every fresh scaffold emits."""
        project = make_project(
            tmp_path, "alpha", config_text="context: repo\nproject:\n  name: alpha\n"
        )
        descriptor = load_descriptor(project)
        assert descriptor is not None and descriptor.context == "repo"

    def test_context_ambient_is_read(self, tmp_path: Path) -> None:
        """M18: the owner opting back in to the ambient layer."""
        project = make_project(
            tmp_path, "alpha", config_text="context: ambient\nproject:\n  name: alpha\n"
        )
        descriptor = load_descriptor(project)
        assert descriptor is not None and descriptor.context == "ambient"

    def test_scope_project_is_repo(self, tmp_path: Path) -> None:
        """M39: the standard's spelling of a governed repo."""
        project = make_project(
            tmp_path, "alpha", config_text="scope: project\nproject:\n  name: alpha\n"
        )
        descriptor = load_descriptor(project)
        assert descriptor is not None
        assert (descriptor.context, descriptor.marker_key) == ("repo", "scope")

    def test_scope_user_is_ambient(self, tmp_path: Path) -> None:
        """M38: the standard's spelling of the opt-out."""
        project = make_project(
            tmp_path, "alpha", config_text="scope: user\nproject:\n  name: alpha\n"
        )
        descriptor = load_descriptor(project)
        assert descriptor is not None
        assert (descriptor.context, descriptor.marker_key) == ("ambient", "scope")

    def test_either_key_opting_out_decides_ambient(self, tmp_path: Path) -> None:
        """M40: `context: repo` beside `scope: user` is an opt-out, in either order."""
        for text in ("context: repo\nscope: user\n", "scope: user\ncontext: repo\n"):
            project = make_project(
                tmp_path / text[:5], "alpha", config_text=text + "project:\n  name: alpha\n"
            )
            descriptor = load_descriptor(project)
            assert descriptor is not None
            assert (descriptor.context, descriptor.marker_key) == ("ambient", "both")

    def test_legacy_context_is_reported_as_the_old_key(self, tmp_path: Path) -> None:
        project = make_project(
            tmp_path, "alpha", config_text="context: repo\nproject:\n  name: alpha\n"
        )
        descriptor = load_descriptor(project)
        assert descriptor is not None and descriptor.marker_key == "context"

    def test_an_unknown_scope_value_is_ignored_with_a_warning(self, tmp_path: Path) -> None:
        project = make_project(
            tmp_path, "alpha", config_text="scope: repo\nproject:\n  name: alpha\n"
        )
        descriptor = load_descriptor(project)
        assert descriptor is not None and descriptor.context == ""
        assert any("scope 'repo'" in w for w in descriptor.warnings)

    def test_a_nested_scope_key_is_a_different_key(self, tmp_path: Path) -> None:
        """M44."""
        project = make_project(
            tmp_path,
            "alpha",
            config_text="project:\n  name: alpha\ntooling:\n  scope: user\n",
        )
        descriptor = load_descriptor(project)
        assert descriptor is not None and (descriptor.context, descriptor.marker_key) == ("", "")

    def test_an_opted_out_project_is_still_managed(self, tmp_path: Path) -> None:
        """`ambient` says which agent layer governs INSIDE the repo. It is not
        a request to be forgotten by the fleet, and reading it as one would
        silently remove a project from every command."""
        project = make_project(
            tmp_path, "alpha", config_text="context: ambient\nproject:\n  name: alpha\n"
        )
        descriptor = load_descriptor(project)
        assert descriptor is not None
        assert descriptor.name == "alpha"
        assert not descriptor.warnings

    def test_absent_context_is_empty_and_never_ambient(self, tmp_path: Path) -> None:
        """M21: every descriptor scaffolded before PI-901 lacks the key, which
        is why it is optional in the schema. Absence is UNKNOWN — reading it as
        an opt-out would re-label the whole installed base at once."""
        descriptor = load_descriptor(make_project(tmp_path, "alpha"))
        assert descriptor is not None and descriptor.context == ""

    def test_a_nested_context_key_is_a_different_key(self, tmp_path: Path) -> None:
        """M23: ``context`` is top-level. One indented under an unrelated block
        shares a name and nothing else. Real YAML parsing gives this for free —
        the shell and regex readers of the same field have to spell it out."""
        project = make_project(
            tmp_path,
            "alpha",
            config_text="project:\n  name: alpha\ntooling:\n  context: ambient\n",
        )
        descriptor = load_descriptor(project)
        assert descriptor is not None and descriptor.context == ""

    def test_a_commented_out_context_is_not_a_declaration(self, tmp_path: Path) -> None:
        """M22: ``# context: ambient`` is a note someone wrote."""
        project = make_project(
            tmp_path, "alpha", config_text="# context: ambient\nproject:\n  name: alpha\n"
        )
        descriptor = load_descriptor(project)
        assert descriptor is not None and descriptor.context == ""

    def test_a_near_miss_value_is_ignored_with_a_warning(self, tmp_path: Path) -> None:
        """For a key three implementations in two languages read, the realistic
        failure is a value that looks marked to a human and resolves to nothing
        in code. Ignore it — loudly, like heal.mode."""
        project = make_project(
            tmp_path, "alpha", config_text="context: Repo\nproject:\n  name: alpha\n"
        )
        descriptor = load_descriptor(project)
        assert descriptor is not None
        assert descriptor.context == ""
        assert any("context" in w for w in descriptor.warnings)

    def test_a_non_string_context_is_malformed_not_absent(self, tmp_path: Path) -> None:
        """PR #200 review (P2). `context: true` parses as a bool and used to be
        coerced to "" and returned silently — so an invalid boundary marker read
        exactly like a deliberate omission, and absence has a DEFINED meaning
        here (M21: fall back to marker presence). Malformed must be visible."""
        project = make_project(
            tmp_path, "alpha", config_text="context: true\nproject:\n  name: alpha\n"
        )
        descriptor = load_descriptor(project)
        assert descriptor is not None
        assert descriptor.context == ""
        assert any("context" in w for w in descriptor.warnings)

    def test_a_list_valued_context_also_warns(self, tmp_path: Path) -> None:
        """The other shapes YAML will hand back: a sequence, and a mapping."""
        project = make_project(
            tmp_path, "alpha", config_text="context:\n  - repo\nproject:\n  name: alpha\n"
        )
        descriptor = load_descriptor(project)
        assert descriptor is not None
        assert descriptor.context == "" and descriptor.warnings

    def test_a_bare_context_key_is_absent_not_malformed(self, tmp_path: Path) -> None:
        """`context:` with nothing after it is the one shape genuinely
        indistinguishable from an absent key — the writer got as far as the key
        and no further. It must stay silent, or every half-typed config warns."""
        project = make_project(tmp_path, "alpha", config_text="context:\nproject:\n  name: alpha\n")
        descriptor = load_descriptor(project)
        assert descriptor is not None
        assert descriptor.context == "" and not descriptor.warnings

    def test_context_reaches_the_json_surface(self, tmp_path: Path) -> None:
        """The reason to read it at all is to be able to report it. ``asdict``
        is generic, so this pins the field against a future hand-rolled
        serializer that enumerates keys and quietly drops the new one."""
        from dataclasses import asdict

        project = make_project(
            tmp_path, "alpha", config_text="context: ambient\nproject:\n  name: alpha\n"
        )
        descriptor = load_descriptor(project)
        assert descriptor is not None
        assert asdict(descriptor)["context"] == "ambient"


class TestSymlinkedMarkerIsRefused:
    """Marker contract cases M13/M24 — a symlinked marker is refused and the walk continues.

    ``is_file()`` follows symlinks, so a planted ``.agents`` (or an
    ``.agents/config.yaml``) pointing outside the project handed the fleet a
    descriptor authored outside that project's review — and the fleet reads the
    safety allowlist, the expected hooks, the observability path and the heal
    mode from it. project-init closed the same hole in PI-903; this side stayed
    open, measured 2026-08-02.
    """

    def test_a_symlinked_layout_dir_is_refused(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside" / ".agents"
        outside.mkdir(parents=True)
        (outside / "config.yaml").write_text('project:\n  name: "forged"\n', encoding="utf-8")
        project = tmp_path / "victim"
        project.mkdir()
        (project / ".agents").symlink_to(outside, target_is_directory=True)
        assert resolve_config(project) is None
        assert load_descriptor(project) is None

    def test_a_symlinked_config_file_is_refused(self, tmp_path: Path) -> None:
        planted = tmp_path / "outside" / "config.yaml"
        planted.parent.mkdir(parents=True)
        planted.write_text('project:\n  name: "forged"\n', encoding="utf-8")
        project = tmp_path / "victim"
        (project / ".agents").mkdir(parents=True)
        (project / ".agents" / "config.yaml").symlink_to(planted)
        assert resolve_config(project) is None

    def test_a_refused_link_falls_through_to_the_legacy_layout(self, tmp_path: Path) -> None:
        """Refusing must mean 'try the next layout', not 'give up': a planted
        ``.agents`` link must not shadow a real legacy ``.claude`` descriptor."""
        outside = tmp_path / "outside" / ".agents"
        outside.mkdir(parents=True)
        (outside / "config.yaml").write_text('project:\n  name: "forged"\n', encoding="utf-8")
        project = tmp_path / "victim"
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "config.yaml").write_text(
            'project:\n  name: "real"\n', encoding="utf-8"
        )
        (project / ".agents").symlink_to(outside, target_is_directory=True)
        resolved = resolve_config(project)
        assert resolved is not None and resolved[1] == ".claude"
        descriptor = load_descriptor(project)
        assert descriptor is not None and descriptor.name == "real"

    def test_a_real_marker_is_still_discovered(self, fleet_dir: Path) -> None:
        """The case that proves the refusal discriminates rather than simply
        rejecting: every governed repo on a real machine is an ordinary
        directory, and a refusal that caught them would empty the fleet."""
        project = make_project(fleet_dir, "ordinary", layout=".agents")
        resolved = resolve_config(project)
        assert resolved is not None and resolved[1] == ".agents"


# --- An unreadable layout degrades, it does not raise (#210) ---
#
# These inject PermissionError instead of using chmod on purpose. CPython 3.14
# swallows EACCES in pathlib and returns False, so a chmod-based test passes on
# this repo's own venv whether or not the bug is present — it would be a test
# that cannot fail, which is worse than no test. Injection reproduces what the
# 3.13 interpreter a `uv tool install` pins actually does, on every version.


def _raise_eacces(*_args: object, **_kwargs: object) -> bool:
    raise PermissionError(13, "Permission denied")


class TestUnreadableLayoutDegrades:
    def test_an_unreadable_layout_returns_none_rather_than_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "victim"
        (project / ".agents").mkdir(parents=True)
        (project / ".agents" / "config.yaml").write_text(
            'project:\n  name: "real"\n', encoding="utf-8"
        )
        monkeypatch.setattr(Path, "is_symlink", _raise_eacces)
        assert resolve_config(project) is None

    def test_an_unreadable_layout_does_not_break_load_descriptor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "victim"
        (project / ".agents").mkdir(parents=True)
        (project / ".agents" / "config.yaml").write_text(
            'project:\n  name: "real"\n', encoding="utf-8"
        )
        monkeypatch.setattr(Path, "is_file", _raise_eacces)
        assert load_descriptor(project) is None

    def test_an_unreadable_agents_falls_through_to_the_legacy_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``continue``, not ``return``: an unreadable ``.agents`` must not
        shadow a readable legacy ``.claude``, exactly as a refused symlink
        does not."""
        project = tmp_path / "victim"
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "config.yaml").write_text(
            'project:\n  name: "real"\n', encoding="utf-8"
        )
        real_is_file = Path.is_file

        def only_agents_is_unreadable(self: Path) -> bool:
            if ".agents" in self.parts:
                raise PermissionError(13, "Permission denied")
            return real_is_file(self)

        monkeypatch.setattr(Path, "is_file", only_agents_is_unreadable)
        resolved = resolve_config(project)
        assert resolved is not None and resolved[1] == ".claude"


# --- Absent vs present-but-unreadable (#216) ---

_INT_FIELDS_CONFIG = """\
project:
  name: "alpha"
  description: "test project"
  project_init_version: 0.5.2
{version_line}
language: python
delivery: library
memory:
  tier: {tier}
tooling:
  lint_command: "true"
"""


def _int_fields(fleet_dir: Path, name: str, version_line: str = "", tier: str = "0"):
    project = make_project(
        fleet_dir,
        name,
        config_text=_INT_FIELDS_CONFIG.format(version_line=version_line, tier=tier),
    )
    descriptor = load_descriptor(project)
    assert descriptor is not None
    return descriptor


def test_a_malformed_contract_version_is_recorded_as_malformed(fleet_dir: Path) -> None:
    descriptor = _int_fields(fleet_dir, "a", '  project_init_contract_version: "two"')
    assert "project_init_contract_version" in descriptor.malformed


def test_a_malformed_contract_version_is_warned_about(fleet_dir: Path) -> None:
    descriptor = _int_fields(fleet_dir, "b", '  project_init_contract_version: "two"')
    assert descriptor.warnings == (
        "project_init_contract_version is not an integer — ignored: 'two'",
    )


def test_an_absent_contract_version_is_not_called_malformed(fleet_dir: Path) -> None:
    # The control that makes the test above mean anything: absent must stay
    # silent, or "malformed" degenerates into "not v1+" and says nothing new.
    assert _int_fields(fleet_dir, "c").malformed == ()


def test_a_null_contract_version_counts_as_absent(fleet_dir: Path) -> None:
    # `project_init_contract_version:` with no value parses as None. Nothing was
    # declared, so there is nothing to call broken.
    assert _int_fields(fleet_dir, "d", "  project_init_contract_version:").malformed == ()


def test_a_malformed_memory_tier_is_recorded_as_malformed(fleet_dir: Path) -> None:
    # Same coercion, one field over — and this one gates retrieval surfaces.
    assert "memory.tier" in _int_fields(fleet_dir, "e", tier='"deep"').malformed


def test_a_malformed_memory_tier_still_degrades_to_tier_zero(fleet_dir: Path) -> None:
    # Never-raise is preserved: the value is still usable, just now visible.
    assert _int_fields(fleet_dir, "f", tier='"deep"').memory_tier == 0


# --- Values int() reshapes instead of refusing (#228 review) ---
#
# The malformed path above is reached by int() RAISING. A YAML bool and a
# fractional float never raise: `int(True)` is 1 and `int(2.9)` is 2, so a
# schema-invalid value came back as a plausible declared version. That is worse
# than the bug #216 fixed — not a default standing in for an unreadable field,
# but a wrong answer indistinguishable from a right one.


def test_a_boolean_contract_version_is_malformed_not_version_one(
    fleet_dir: Path,
) -> None:
    descriptor = _int_fields(fleet_dir, "bool_v", "  project_init_contract_version: true")
    assert "project_init_contract_version" in descriptor.malformed
    assert descriptor.contract_version == 0


def test_a_fractional_contract_version_is_malformed_not_truncated(
    fleet_dir: Path,
) -> None:
    # 2.9 became v2 and switched on v2 parsing off a value nobody wrote.
    descriptor = _int_fields(fleet_dir, "frac_v", "  project_init_contract_version: 2.9")
    assert "project_init_contract_version" in descriptor.malformed
    assert descriptor.contract_version == 0


def test_a_boolean_memory_tier_is_malformed(fleet_dir: Path) -> None:
    assert "memory.tier" in _int_fields(fleet_dir, "bool_t", tier="true").malformed


def test_a_fractional_memory_tier_is_malformed(fleet_dir: Path) -> None:
    descriptor = _int_fields(fleet_dir, "frac_t", tier="2.9")
    assert "memory.tier" in descriptor.malformed
    assert descriptor.memory_tier == 0


def test_a_reshaped_contract_version_reaches_the_renderer_as_bad(
    fleet_dir: Path,
) -> None:
    # Recording it is only half: contract_label branches on `malformed`, so this
    # is what the operator actually sees instead of a confident `v1`.
    descriptor = _int_fields(fleet_dir, "bool_lbl", "  project_init_contract_version: true")
    assert contract_label(descriptor) == "bad"


def test_an_integral_float_version_stays_valid(fleet_dir: Path) -> None:
    # The control that keeps the four above from meaning "reject anything
    # unusual". JSON Schema counts 2.0 as an integer, and YAML types an
    # unquoted 2.0 as a float, so a refusal here would invent a rule the
    # contract does not have and fire on a correct config.
    descriptor = _int_fields(fleet_dir, "float_ok", "  project_init_contract_version: 2.0")
    assert descriptor.malformed == ()
    assert descriptor.contract_version == 2
    assert contract_label(descriptor) == "v2"


def test_an_integral_float_memory_tier_stays_valid(fleet_dir: Path) -> None:
    descriptor = _int_fields(fleet_dir, "float_ok_t", tier="1.0")
    assert descriptor.malformed == ()
    assert descriptor.memory_tier == 1


# --- contract_label: one rendering, replacing three divergent ones ---


def test_contract_label_says_bad_for_a_declared_unreadable_version(fleet_dir: Path) -> None:
    descriptor = _int_fields(fleet_dir, "g", '  project_init_contract_version: "two"')
    assert contract_label(descriptor) == "bad"


def test_contract_label_says_none_for_an_absent_version(fleet_dir: Path) -> None:
    assert contract_label(_int_fields(fleet_dir, "h")) == "none"


def test_contract_label_renders_a_negative_version_rather_than_denying_it(
    fleet_dir: Path,
) -> None:
    # `none` would claim the field is absent. It is present and readable — it is
    # just not a version. fleet.py said `none` here while detail.py said `v-3`.
    descriptor = _int_fields(fleet_dir, "i", "  project_init_contract_version: -3")
    assert contract_label(descriptor) == "v-3"


def test_contract_label_renders_a_good_version(fleet_dir: Path) -> None:
    assert contract_label(_int_fields(fleet_dir, "j", "  project_init_contract_version: 2")) == "v2"


# --- #257 (project-init #960): the stack decides the tier -------------------

_BOTH_SURFACES = "  vault_path: .claude/vault\n  graph_path: graphify-out/graph.json\n"


def test_a_tier_that_contradicts_its_stack_reads_the_stacks_surfaces(tmp_path: Path) -> None:
    # project-init #960's reproduction: a tier-2 child with `tier` edited to 0.
    # Gating on the edited tier dropped the graph and the vault.
    descriptor = parse_config(
        _memory_config(0, _BOTH_SURFACES, stack="obsidian-graphify"), tmp_path
    )
    assert descriptor.memory_tier == 2
    assert descriptor.graph_path is not None
    assert descriptor.vault_path is not None


def test_a_tier_that_contradicts_its_stack_is_reported(tmp_path: Path) -> None:
    descriptor = parse_config(_memory_config(0, stack="obsidian-graphify"), tmp_path)
    assert descriptor.warnings == (
        "memory.tier 0 disagrees with memory.stack 'obsidian-graphify' (tier 2) — "
        "reading tier 2; the stack is the source of truth",
    )


def test_a_tier_that_agrees_with_its_stack_is_silent(tmp_path: Path) -> None:
    # The control: without it, a reader that warned on every memory block would
    # pass the test above.
    for tier in range(4):
        assert parse_config(_memory_config(tier), tmp_path).warnings == (), tier


def test_the_legacy_obsidian_alias_is_tier_one(tmp_path: Path) -> None:
    descriptor = parse_config(
        _memory_config(1, "  vault_path: .claude/vault\n", stack="obsidian"), tmp_path
    )
    assert (descriptor.memory_tier, descriptor.warnings) == (1, ())
    assert descriptor.vault_path is not None


def test_a_stack_off_the_ladder_keeps_the_declared_tier(tmp_path: Path) -> None:
    # Nothing to derive from, so the declared tier is the only evidence — and a
    # stack newer than this reader must not be called a contradiction.
    descriptor = parse_config(_memory_config(2, stack="some-future-stack"), tmp_path)
    assert (descriptor.memory_tier, descriptor.warnings) == (2, ())


def test_a_block_without_a_stack_keeps_the_declared_tier(tmp_path: Path) -> None:
    text = "project:\n  name: a\nmemory:\n  tier: 2\n  memory_path: .claude/memory\n"
    descriptor = parse_config(text, tmp_path)
    assert (descriptor.memory_tier, descriptor.memory_stack) == (2, "unknown")


def test_a_malformed_tier_beside_a_known_stack_is_malformed_not_contradictory(
    tmp_path: Path,
) -> None:
    descriptor = parse_config(_memory_config('"deep"', stack="obsidian-graphify"), tmp_path)
    assert "memory.tier" in descriptor.malformed
    assert descriptor.memory_tier == 2
    assert len(descriptor.warnings) == 1  # the malformed value, not a second "disagrees"


def test_no_memory_block_under_the_contract_reads_as_declined(tmp_path: Path) -> None:
    # A `core` scaffold renders no memory block; project-init's reader rule says
    # that absence IS the declaration at contract v1+ (#964 rung 2).
    for version in (1, 2):
        text = f"project:\n  name: a\n  project_init_contract_version: {version}\n"
        assert parse_config(text, tmp_path).memory_stack == MEMORY_STACK_NONE, version


def test_a_present_block_without_a_stack_is_not_read_as_declined(tmp_path: Path) -> None:
    text = "project:\n  name: a\n  project_init_contract_version: 2\nmemory:\n  tier: 0\n"
    assert parse_config(text, tmp_path).memory_stack == "unknown"

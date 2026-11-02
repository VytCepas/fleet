"""The versions drift check (box register, versions): a repo that pins differently from the register is reported.

Repos are built in ``tmp_path``; the register is a small literal in the shape of the ambient layer's
``standards/naming/versions.tsv``. The assertions are on the parsed findings, not on text.
"""

from __future__ import annotations

from pathlib import Path

from projects_orchestrator import versions

_SHA = "df4cb1c069e1874edd31b4311f1884172cec0e10"
_OTHER = "0123456789abcdef0123456789abcdef01234567"
_REGISTER = (
    "# versions\n"
    "id\taxis\tname\tvalue\tsha\thold_at\thold_reason\tupgrade\tnote\n"
    "runtime.python-floor\truntime\tpython-floor\t3.11\t-\t-\t-\tmanual\t-\n"
    "runtime.python-version-file\truntime\tpython-version-file\t3.13\t-\t-\t-\tmanual\t-\n"
    "tool.ruff\ttool\truff\t0.14\t-\t-\t-\tpr\t-\n"
    "library.typescript\tlibrary\ttypescript\t5\t-\t-\t-\tpr\t-\n"
    "tool.bun\ttool\tbun\t1.4\t-\t-\t-\tmanual\t-\n"
    f"action.actions-checkout\taction\tactions/checkout\tv6.0.3\t{_SHA}\t-\t-\tpr\t-\n"
)


def _repo(tmp_path: Path, name: str, **files: str) -> Path:
    """A repo directory holding ``files`` (keys use ``__`` for ``/`` and ``_dot_`` for a leading dot)."""
    root = tmp_path / name
    root.mkdir()
    for key, text in files.items():
        path = root / key.replace("__", "/").replace("_dot_", ".")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _workflow(*uses: str) -> str:
    """A workflow file with the given ``uses:`` values."""
    steps = "".join(f"      - uses: {u}\n" for u in uses)
    return f"name: x\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n{steps}"


def _drift(root: Path) -> dict[str, versions.Drift]:
    """The repo's drift keyed by register id."""
    pins = versions.load_register(_REGISTER)
    return {d.id: d for d in versions.drift(root.name, pins, versions.inspect(root))}


def test_a_repo_that_matches_the_register_has_no_drift(tmp_path: Path) -> None:
    """Every pin equals the register and a bot is present: only the bot's ok row remains."""
    root = _repo(
        tmp_path,
        "clean",
        **{
            "pyproject.toml": '[project]\nrequires-python = ">=3.11"\n[dependency-groups]\ndev = ["ruff>=0.14"]\n',
            "_dot_python-version": "3.13\n",
            "renovate.json": "{}",
            "_dot_github__workflows__ci.yml": _workflow(f"actions/checkout@{_SHA} # v6"),
        },
    )
    assert {k: v.status for k, v in _drift(root).items()} == {"update-bot": "ok"}


def test_a_mutable_tag_and_another_commit_are_each_drift(tmp_path: Path) -> None:
    """The mutable-tag case: ``@v5`` is a mutable tag; a different full SHA is also not the registered one."""
    tag = _repo(
        tmp_path, "tag", **{"_dot_github__workflows__a.yml": _workflow("actions/checkout@v5")}
    )
    sha = _repo(
        tmp_path,
        "sha",
        **{"_dot_github__workflows__a.yml": _workflow(f"actions/checkout@{_OTHER} # v4")},
    )
    assert "mutable tag" in _drift(tag)["action.actions-checkout"].found
    assert _drift(sha)["action.actions-checkout"].found == _OTHER[:7]


def test_the_fleet_case_a_python_file_that_disagrees_and_a_low_ruff(tmp_path: Path) -> None:
    """The interpreter file is compared exactly; a tool floor below the register is drift, above is not."""
    root = _repo(
        tmp_path,
        "fleet",
        **{
            "pyproject.toml": '[project]\nrequires-python = ">=3.11"\n[dependency-groups]\ndev = ["ruff>=0.4"]\n',
            "_dot_python-version": "3.14.6\n",
        },
    )
    found = _drift(root)
    assert found["runtime.python-version-file"].found == "3.14"
    assert found["tool.ruff"].found == "0.4"
    assert "runtime.python-floor" not in found
    above = _repo(
        tmp_path, "above", **{"pyproject.toml": '[project]\ndependencies = ["ruff>=0.20"]\n'}
    )
    assert "tool.ruff" not in _drift(above)


def test_an_unpinned_tool_and_a_latest_bun_are_drift(tmp_path: Path) -> None:
    """``ruff`` with no bound and ``bun-version: latest`` pin nothing."""
    root = _repo(
        tmp_path,
        "loose",
        **{
            "pyproject.toml": '[project]\ndependencies = ["ruff"]\n',
            "_dot_github__workflows__a.yml": "jobs:\n  j:\n    steps:\n      - with:\n          bun-version: latest\n",
        },
    )
    found = _drift(root)
    assert found["tool.ruff"].found == "unpinned"
    assert found["tool.bun"].found == "latest"


def test_an_action_the_register_does_not_name_is_drift(tmp_path: Path) -> None:
    """The version-registered rule: a pin held in one place only."""
    root = _repo(
        tmp_path, "x", **{"_dot_github__workflows__a.yml": _workflow(f"some/other@{_OTHER} # v1")}
    )
    assert "action:some/other" in _drift(root)


def test_no_update_bot_is_drift_until_an_exception_row_covers_it(tmp_path: Path) -> None:
    """The coverage check lists a repo with neither a bot nor an exemption."""
    root = _repo(tmp_path, "nobot")
    pins = versions.load_register(_REGISTER)
    rows = versions.report({"nobot": root}, pins, [])
    assert [(r.id, r.status) for r in rows] == [("update-bot", "drift")]
    covered = versions.report(
        {"nobot": root}, pins, [("nobot", "update-bot", "no dependencies to bump")]
    )
    assert [(r.id, r.status, r.reason) for r in covered] == [
        ("update-bot", "excepted", "no dependencies to bump")
    ]


def test_a_star_row_exempts_a_whole_repo_with_its_reason(tmp_path: Path) -> None:
    """A repo out of scope is listed once, exempt, and its pins are not read."""
    root = _repo(
        tmp_path, "later", **{"_dot_github__workflows__a.yml": _workflow("actions/checkout@v5")}
    )
    rows = versions.report(
        {"later": root}, versions.load_register(_REGISTER), [("later", "*", "retires next week")]
    )
    assert [(r.id, r.status, r.reason) for r in rows] == [("*", "exempt", "retires next week")]


def test_the_command_exits_one_on_open_drift_and_zero_when_all_is_excepted(tmp_path: Path) -> None:
    """The exit code is the verdict: drift with no exception row is 1."""
    register = tmp_path / "versions.tsv"
    register.write_text(_REGISTER, encoding="utf-8")
    root = _repo(tmp_path, "r", **{"renovate.json": "{}", "_dot_python-version": "3.12\n"})
    argv = ["--register", str(register), "--repo", f"r={root}"]
    assert versions.main(argv) == 1
    exceptions = tmp_path / "exceptions.tsv"
    exceptions.write_text(
        "repo\tid\treason\nr\truntime.python-version-file\ton 3.12 until the base image moves\n",
        encoding="utf-8",
    )
    assert versions.main([*argv, "--exceptions", str(exceptions)]) == 0

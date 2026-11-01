"""`test-fleet` (#316): every fleet repo's `just test`, one row per repo, a red fleet exits 1.

The fleets here are planted repos whose `justfile` prints the contract's summary
line and exits as told, so the command's reading of real `just` runs is what is
checked — never a stubbed runner.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from projects_orchestrator import testfleet
from projects_orchestrator.__main__ import main
from projects_orchestrator.testfleet import parse_summary
from tests.conftest import git_init, make_project

#: The registry id, spelled out: importing the module's own constant would let a
#: changed stamp change the expectation with it.
STAMP = "[check:test.fleet]"

pytestmark = pytest.mark.skipif(
    shutil.which("just") is None and not os.environ.get("CI"),
    reason="needs just; CI installs it, so there it runs",
)


def _repo(fleet: Path, name: str, recipe: str | None) -> Path:
    """A project whose justfile's `test` recipe runs *recipe* (None: no justfile)."""
    project = make_project(fleet, name)
    if recipe is not None:
        (project / "justfile").write_text(f"test:\n    @sh -c '{recipe}'\n", encoding="utf-8")
    return project


def _run(fleet: Path, *extra: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    rc = main(["test-fleet", "--root", str(fleet), *extra])
    return rc, capsys.readouterr().out


def _rows(out: str) -> dict[str, list[str]]:
    rows = {}
    for line in out.splitlines():
        fields = line.split()
        if (
            len(fields) >= 6
            and fields[0] == STAMP
            and fields[1] != "repo"
            and not fields[1].endswith(":")
        ):
            rows[fields[1]] = fields[2:6]
    return rows


def test_one_red_repo_makes_the_fleet_exit_1(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(fleet_dir, "alpha", "echo alpha: 3 passed, 0 failed")
    _repo(fleet_dir, "beta", "echo beta: 1 passed, 2 failed; exit 1")
    rc, out = _run(fleet_dir, capsys=capsys)
    assert rc == 1, out
    rows = _rows(out)
    assert rows["alpha"] == ["3", "0", "0", "pass"], out
    assert rows["beta"] == ["1", "2", "1", "fail"], out


def test_an_all_green_fleet_exits_0(fleet_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _repo(fleet_dir, "alpha", "echo alpha: 3 passed, 0 failed")
    _repo(fleet_dir, "gamma", "echo gamma: 5 passed, 0 failed")
    rc, out = _run(fleet_dir, capsys=capsys)
    assert rc == 0, out
    assert (
        out.strip().splitlines()[-1]
        == f"{STAMP} test.fleet: 2 repos, 2 pass, 0 fail, 0 skip; 8 passed, 0 failed"
    )


def test_every_line_carries_the_check_id(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(fleet_dir, "alpha", "echo alpha: 3 passed, 0 failed")
    _repo(fleet_dir, "beta", "echo beta: 0 passed, 1 failed; exit 1")
    _repo(fleet_dir, "delta", None)
    _, out = _run(fleet_dir, capsys=capsys)
    lines = out.strip().splitlines()
    assert len(lines) == 5, out  # header, three rows, summary
    assert all(line.startswith(STAMP + " ") for line in lines), out


def test_a_repo_without_a_test_recipe_is_red_not_skipped(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#967 item 5: a governed repo with no `test` recipe is a red row, never a skip."""
    _repo(fleet_dir, "alpha", "echo alpha: 3 passed, 0 failed")
    _repo(fleet_dir, "delta", None)
    (_repo(fleet_dir, "eps", None) / "justfile").write_text("lint:\n    @true\n", encoding="utf-8")
    rc, out = _run(fleet_dir, capsys=capsys)
    assert rc == 1, out
    rows = _rows(out)
    assert rows["alpha"][-1] == "pass", out
    assert rows["delta"] == ["?", "?", "-", "fail"], out
    assert rows["eps"] == ["?", "?", "-", "fail"], out
    assert "no justfile" in out and "no `test` recipe" in out, out


def test_a_plain_repo_without_a_test_recipe_is_skipped_not_red(
    fleet_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#967 item 5: the red rule covers governed repos only. A plain git repo (no
    project-init descriptor, admitted by include_plain_repos) keeps its skip."""
    _repo(fleet_dir, "alpha", "echo alpha: 3 passed, 0 failed")
    plain = fleet_dir / "legacy"
    plain.mkdir()
    git_init(plain, commit=False)
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text(f"roots:\n  - {fleet_dir}\ninclude_plain_repos: true\n", encoding="utf-8")
    rc = main(["test-fleet", "--fleet", str(fleet_file)])
    out = capsys.readouterr().out
    assert rc == 0, out
    rows = _rows(out)
    assert rows["legacy"] == ["-", "-", "-", "skip"], out
    assert rows["alpha"][-1] == "pass", out


def test_a_just_that_cannot_list_the_recipes_is_red_not_skipped(
    fleet_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #320 review: `just` absent (127) must not read as "no test recipe"."""
    _repo(fleet_dir, "alpha", "echo alpha: 3 passed, 0 failed")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    rc, out = _run(fleet_dir, capsys=capsys)
    assert rc == 1, out
    assert _rows(out)["alpha"] == ["?", "?", "-", "fail"], out
    assert "just --summary" in out and "127" in out, out


def test_a_malformed_justfile_is_red_not_skipped(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """PR #320 review: a justfile `just` cannot parse declares a gate it cannot run."""
    (_repo(fleet_dir, "broken", None) / "justfile").write_text(
        "test\n    @echo x\n", encoding="utf-8"
    )
    rc, out = _run(fleet_dir, capsys=capsys)
    assert rc == 1, out
    assert _rows(out)["broken"] == ["?", "?", "-", "fail"], out


def test_a_hung_recipe_listing_is_red_not_skipped(
    fleet_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #320 review: a `just --summary` that times out is a failure too."""
    _repo(fleet_dir, "alpha", "echo alpha: 3 passed, 0 failed")
    bin_dir = tmp_path / "hang-bin"
    bin_dir.mkdir()
    hang = bin_dir / "just"
    hang.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    hang.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(testfleet, "_RECIPE_TIMEOUT", 1.0)
    rc, out = _run(fleet_dir, capsys=capsys)
    assert rc == 1, out
    assert _rows(out)["alpha"] == ["?", "?", "-", "fail"], out
    assert "timed out" in out, out


def test_the_last_summary_line_across_both_streams_is_the_count(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """PR #320 review: the last line printed decides, whichever stream carried it."""
    _repo(
        fleet_dir,
        "late-err",
        "echo part: 1 passed, 0 failed; echo late-err: 1 passed, 2 failed >&2",
    )
    _repo(
        fleet_dir,
        "late-out",
        "echo part: 9 passed, 9 failed >&2; echo late-out: 3 passed, 0 failed",
    )
    rc, out = _run(fleet_dir, capsys=capsys)
    assert rc == 1, out
    rows = _rows(out)
    assert rows["late-err"] == ["1", "2", "0", "fail"], out
    assert rows["late-out"] == ["3", "0", "0", "pass"], out


def test_the_last_summary_line_is_the_count(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(fleet_dir, "alpha", "echo s1: 1 passed, 9 failed; echo alpha: 4 passed, 0 failed")
    rc, out = _run(fleet_dir, capsys=capsys)
    assert rc == 0, out
    assert _rows(out)["alpha"] == ["4", "0", "0", "pass"], out


def test_exit_0_with_a_counted_failure_is_red(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(fleet_dir, "liar", "echo liar: 2 passed, 1 failed")
    rc, out = _run(fleet_dir, capsys=capsys)
    assert rc == 1, out
    assert _rows(out)["liar"] == ["2", "1", "0", "fail"], out


def test_no_summary_line_is_unknown_counts_and_the_exit_decides(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(fleet_dir, "quiet", "echo all good")
    _repo(fleet_dir, "crash", "echo boom >&2; exit 3")
    rc, out = _run(fleet_dir, capsys=capsys)
    assert rc == 1, out
    rows = _rows(out)
    assert rows["quiet"] == ["?", "?", "0", "pass"], out
    assert rows["crash"] == ["?", "?", "3", "fail"], out
    assert "no summary line" in out


def test_a_hung_suite_is_killed_and_red(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(fleet_dir, "hang", "sleep 30")
    rc, out = _run(fleet_dir, "--timeout", "2", capsys=capsys)
    assert rc == 1, out
    assert _rows(out)["hang"] == ["?", "?", "-", "fail"], out
    assert "timed out" in out


def test_json_carries_the_rows_and_the_exit(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(fleet_dir, "alpha", "echo alpha: 3 passed, 0 failed")
    _repo(fleet_dir, "beta", "echo beta: 1 passed, 2 failed; exit 1")
    rc, out = _run(fleet_dir, "--json", capsys=capsys)
    assert rc == 1
    doc = json.loads(out)
    assert doc["check"] == "test.fleet"
    by_repo = {row["repo"]: row for row in doc["rows"]}
    assert by_repo["beta"]["failed"] == 2
    assert by_repo["beta"]["exit"] == 1
    assert by_repo["alpha"]["status"] == "pass"


def test_an_empty_fleet_is_not_a_green_fleet(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fleet_dir.mkdir(parents=True)
    rc, out = _run(fleet_dir, capsys=capsys)
    assert rc == 2
    assert out.strip().startswith(STAMP)


def test_discovery_warnings_carry_the_check_id_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text(f"roots:\n  - {tmp_path / 'missing'}\n", encoding="utf-8")
    rc = main(["test-fleet", "--fleet", str(fleet_file)])
    captured = capsys.readouterr()
    assert rc == 2
    lines = (captured.out + captured.err).strip().splitlines()
    assert any("cannot scan root" in line for line in lines), lines
    assert all(line.startswith(STAMP + " ") for line in lines), lines


def test_an_unscanned_root_fails_the_fleet_even_with_a_healthy_sibling(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#320 review: a healthy sibling root filling `selected` must not mask the
    gap left by a root discovery could not read — that root's own projects, if
    any, never got a `just test` run."""
    healthy = tmp_path / "healthy"
    _repo(healthy, "alpha", "echo alpha: 1 passed, 0 failed")
    missing = tmp_path / "missing"
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text(f"roots:\n  - {healthy}\n  - {missing}\n", encoding="utf-8")
    rc = main(["test-fleet", "--fleet", str(fleet_file)])
    captured = capsys.readouterr()
    assert rc == 1, captured.out + captured.err
    lines = (captured.out + captured.err).strip().splitlines()
    assert any("cannot scan root" in line and str(missing) in line for line in lines), lines
    assert _rows(captured.out)["alpha"][-1] == "pass", captured.out


def test_a_dropped_explicit_project_fails_the_fleet_even_with_a_healthy_sibling(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#320 review: an explicit `projects:` entry discover() drops (missing,
    unreadable, refused, or not a project-init project — not a project-init
    project, here) must fail the fleet even though a healthy scanned sibling
    fills `selected`. Checked structurally (configured paths against admitted
    descriptors), not by matching a specific warning's text — the earlier fix
    only recognized the `cannot scan root` prefix."""
    roots = tmp_path / "roots"
    _repo(roots, "alpha", "echo alpha: 1 passed, 0 failed")
    not_a_project = tmp_path / "not-a-project"
    not_a_project.mkdir()
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text(
        f"roots:\n  - {roots}\nprojects:\n  - {not_a_project}\n", encoding="utf-8"
    )
    rc = main(["test-fleet", "--fleet", str(fleet_file)])
    captured = capsys.readouterr()
    assert rc == 1, captured.out + captured.err
    lines = (captured.out + captured.err).strip().splitlines()
    assert any(
        "configured but not tested" in line and str(not_a_project) in line for line in lines
    ), lines
    assert _rows(captured.out)["alpha"][-1] == "pass", captured.out


def test_filtered_run_ignores_an_unrelated_unscanned_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#323: a sibling root discover() could not scan must not fail a run
    filtered to a project whose own root scanned fine."""
    healthy = tmp_path / "healthy"
    _repo(healthy, "alpha", "echo alpha: 1 passed, 0 failed")
    missing = tmp_path / "missing"
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text(f"roots:\n  - {healthy}\n  - {missing}\n", encoding="utf-8")
    rc = main(["test-fleet", "--fleet", str(fleet_file), "alpha"])
    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    assert set(_rows(captured.out)) == {"alpha"}


def test_filtered_run_ignores_an_unrelated_dropped_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#323: an unrelated dropped `projects:` entry must not fail a run
    filtered to a project that was admitted."""
    roots = tmp_path / "roots"
    _repo(roots, "alpha", "echo alpha: 1 passed, 0 failed")
    not_a_project = tmp_path / "not-a-project"
    not_a_project.mkdir()
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text(
        f"roots:\n  - {roots}\nprojects:\n  - {not_a_project}\n", encoding="utf-8"
    )
    rc = main(["test-fleet", "--fleet", str(fleet_file), "alpha"])
    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    assert set(_rows(captured.out)) == {"alpha"}


def test_filtered_run_still_fails_when_its_own_project_never_scanned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#323: scoping fleet-completeness to the selection must not hide a gap
    in the selection itself — its own root failing to scan means it was never
    discovered, so filtering by its name still fails the run."""
    missing = tmp_path / "missing"
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text(f"roots:\n  - {missing}\n", encoding="utf-8")
    rc = main(["test-fleet", "--fleet", str(fleet_file), "alpha"])
    captured = capsys.readouterr()
    assert rc == 2, captured.out + captured.err


def test_one_project_by_name(fleet_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _repo(fleet_dir, "alpha", "echo alpha: 3 passed, 0 failed")
    _repo(fleet_dir, "beta", "echo beta: 1 passed, 2 failed; exit 1")
    rc, out = _run(fleet_dir, "alpha", capsys=capsys)
    assert rc == 0, out
    assert set(_rows(out)) == {"alpha"}


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("x: 1 passed, 0 failed\n", ("x", 1, 0)),
        ("a: 1 passed, 1 failed\nb: 2 passed, 0 failed\n", ("b", 2, 0)),
        ("browser --self-test: 10 passed, 0 failed\n", None),
        ("x smoke: 1 passed, 0 failed (NOT a full run)\n", None),
        ("1 failed, 2 passed in 0.1s\n", None),
    ],
)
def test_parse_summary_takes_the_last_contract_line(
    text: str, want: tuple[str, int, int] | None
) -> None:
    assert parse_summary(text) == want


def test_project_filter_is_case_insensitive(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#322: `test-fleet alpha` said "unknown project" for a project named `Alpha`."""
    _repo(fleet_dir, "Alpha", "echo Alpha: 1 passed, 0 failed")
    rc, out = _run(fleet_dir, "alpha", capsys=capsys)
    assert (rc, set(_rows(out))) == (0, {"Alpha"})

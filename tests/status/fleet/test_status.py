"""Git health collection against real repositories."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import git_init, make_project

from projects_orchestrator import status as status_module
from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.runner import run_command
from projects_orchestrator.status import ProjectStatus, collect_status


def _descriptor(fleet_dir: Path, name: str = "alpha"):
    return load_descriptor(fleet_dir / name)


def test_collect_status_reads_branch(fleet_dir: Path) -> None:
    git_init(make_project(fleet_dir, "alpha"))
    assert collect_status(_descriptor(fleet_dir)).branch == "main"


def test_collect_status_clean_repo_health(fleet_dir: Path) -> None:
    git_init(make_project(fleet_dir, "alpha"))
    assert collect_status(_descriptor(fleet_dir)).health == "clean"


def test_collect_status_detects_dirty_worktree(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    git_init(project)
    (project / "scratch.txt").write_text("wip", encoding="utf-8")
    assert collect_status(_descriptor(fleet_dir)).health == "dirty"


def test_collect_status_non_git_is_unknown(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert collect_status(_descriptor(fleet_dir)).health == "unknown"


def test_status_health_unknown_when_dirty_unreadable() -> None:
    from projects_orchestrator.status import ProjectStatus

    assert ProjectStatus(project="x", branch="main", dirty=None).health == "unknown"


def test_collect_status_non_git_has_detail(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert collect_status(_descriptor(fleet_dir)).detail == "not a git repository"


def test_collect_status_reports_last_commit(fleet_dir: Path) -> None:
    git_init(make_project(fleet_dir, "alpha"))
    assert collect_status(_descriptor(fleet_dir)).last_commit is not None


def test_collect_status_no_upstream_means_unknown_ahead(fleet_dir: Path) -> None:
    git_init(make_project(fleet_dir, "alpha"))
    assert collect_status(_descriptor(fleet_dir)).ahead is None


def test_collect_status_ahead_of_upstream(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    git_init(project)
    clone = fleet_dir / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(project), str(clone)], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(clone),
            "-c",
            "user.email=t@e.c",
            "-c",
            "user.name=T",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "ahead",
        ],
        check=True,
        capture_output=True,
    )
    descriptor = load_descriptor(project).__class__(name="clone", path=clone)
    assert collect_status(descriptor).ahead == 1


# --- a probe killed at its timeout must not strand .git/index.lock ----------------
#
# A plain `git status` holds .git/index.lock while it refreshes the index. `_git`
# SIGKILLs the whole process group at GIT_TIMEOUT, so a status that stalls inside
# that window (a loaded box, a big tree) leaves a 0-byte lock that blocks every git
# write in the repo. The shim freezes the REAL git (SIGSTOP) the moment the lock
# exists, so the kill lands inside the window instead of by luck; the control test
# proves the shim catches it, or "no lock" would pass vacuously.

_STALL_SHIM = """#!/bin/sh
case " $* " in *" status "*) ;; *) exec "$LK_REAL_GIT" "$@" ;; esac
"$LK_REAL_GIT" "$@" &
p=$!
while kill -0 "$p" 2>/dev/null; do
  if [ -e "$LK_LOCK" ]; then kill -STOP "$p"; : > "$LK_STALLED"; sleep 30; kill -CONT "$p"; break; fi
done
wait "$p"
"""
_ATTEMPTS = 4


def _locks_stranded(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe: Callable[[], object]
) -> int:
    """Run ``probe`` ``_ATTEMPTS`` times under the stalling git shim; count the runs that strand a lock."""
    real_git = shutil.which("git")
    assert real_git is not None
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(_STALL_SHIM, encoding="utf-8")
    shim.chmod(0o755)
    lock, stalled = project / ".git" / "index.lock", tmp_path / "stalled"
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("LK_REAL_GIT", real_git)
    monkeypatch.setenv("LK_LOCK", str(lock))
    monkeypatch.setenv("LK_STALLED", str(stalled))
    monkeypatch.setattr(status_module, "GIT_TIMEOUT", 1.5)
    left = 0
    for _ in range(_ATTEMPTS):
        for tracked in project.glob("f*"):
            os.utime(tracked)  # stale every stat: the refresh then has work to do
        lock.unlink(missing_ok=True)
        stalled.unlink(missing_ok=True)
        probe()
        left += lock.exists()
        lock.unlink(missing_ok=True)
    return left


def _repo_with_files(fleet_dir: Path) -> Path:
    project = make_project(fleet_dir, "alpha")
    for i in range(300):
        (project / f"f{i}").write_text(str(i), encoding="utf-8")
    git_init(project)
    return project


def test_control_plain_status_killed_in_lock_window_leaves_the_lock(
    fleet_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _repo_with_files(fleet_dir)

    def probe() -> object:
        return run_command("git status --porcelain", cwd=project, timeout=1.5)

    assert _locks_stranded(project, tmp_path, monkeypatch, probe) >= 1


def test_collect_status_killed_at_its_timeout_strands_no_index_lock(
    fleet_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _repo_with_files(fleet_dir)
    descriptor = load_descriptor(project)

    def probe() -> ProjectStatus:
        return collect_status(descriptor)

    assert _locks_stranded(project, tmp_path, monkeypatch, probe) == 0

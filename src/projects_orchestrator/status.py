"""Per-project git health, degraded gracefully to ``unknown``.

Answers "is this project in a sane state?" without running its gates:
current branch, dirty worktree, ahead/behind upstream, last commit time.
Every git call is timeout-bounded and failure becomes ``unknown`` — the
fleet view must render even for a corrupted or non-git directory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.runner import run_command

_log = logging.getLogger(__name__)

GIT_TIMEOUT = 15.0


@dataclass(frozen=True)
class ProjectStatus:
    """Git health of one project.

    Attributes:
        project: Project name.
        branch: Current branch, or ``None`` when unknown.
        dirty: Whether the worktree has uncommitted changes (``None`` unknown).
        ahead: Commits ahead of upstream (``None`` when no upstream/unknown).
        behind: Commits behind upstream (``None`` when no upstream/unknown).
        last_commit: ISO timestamp of the last commit, or ``None``.
        detail: Human-readable explanation when health is degraded.
    """

    project: str
    branch: str | None = None
    dirty: bool | None = None
    ahead: int | None = None
    behind: int | None = None
    last_commit: str | None = None
    detail: str = ""

    @property
    def health(self) -> str:
        """One-word health summary: clean | dirty | diverged | behind | ahead | unknown."""
        if self.branch is None or self.dirty is None:
            return "unknown"
        if self.dirty:
            return "dirty"
        if self.ahead and self.behind:
            return "diverged"
        if self.behind:
            return "behind"
        if self.ahead:
            return "ahead"
        return "clean"


def _git(path: Path, *args: str) -> str | None:
    """Run one git subcommand in ``path``; ``None`` on any failure.

    Every probe here only reads, so ``--no-optional-locks``: a plain ``git status``
    takes ``.git/index.lock`` to refresh the index, and the SIGKILL at the timeout
    (a loaded box, a big tree) strands it, blocking every git write in that repo.
    """
    result = run_command("git --no-optional-locks " + " ".join(args), cwd=path, timeout=GIT_TIMEOUT)
    return result.stdout.strip() if result.ok else None


def published_default_head(path: Path) -> str:
    """The commit ``origin``'s default branch points at NOW; ``""`` when unknown.

    Asked of the remote (``git ls-remote origin HEAD``), not read from the local
    ``refs/remotes/origin/HEAD``. That ref is only as fresh as the last fetch, so
    after another checkout pushes, a stale clone's HEAD would still match it and
    a gate result for superseded code would pass as current (Codex on #293). The
    arguments are constants, so no remote-supplied name reaches the shell string
    :func:`_git` runs. Offline, unreachable or no ``origin``: ``""``.

    Args:
        path: The repository to ask.

    Returns:
        The full SHA, or ``""``.
    """
    listing = _git(path, "ls-remote", "origin", "HEAD")
    for line in (listing or "").splitlines():
        sha, _, ref = line.partition("\t")
        if ref == "HEAD" and len(sha) == 40:
            return sha
    return ""


def _ahead_behind(path: Path) -> tuple[int | None, int | None]:
    """Return (ahead, behind) relative to upstream, or (None, None)."""
    counts = _git(path, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if counts is None:
        return None, None
    try:
        behind_str, ahead_str = counts.split()
        return int(ahead_str), int(behind_str)
    except ValueError as exc:
        _log.debug("unparseable ahead/behind counts %r: %r", counts, exc)
        return None, None


def collect_status(descriptor: ProjectDescriptor) -> ProjectStatus:
    """Collect git health for one project; never raises.

    Args:
        descriptor: The project to inspect.

    Returns:
        The project's status; a non-git or unreadable directory yields
        ``health == "unknown"`` with an explanatory detail.
    """
    path = descriptor.path
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        return ProjectStatus(project=descriptor.name, detail="not a git repository")

    porcelain = _git(path, "status", "--porcelain")
    ahead, behind = _ahead_behind(path)
    return ProjectStatus(
        project=descriptor.name,
        branch=branch,
        dirty=None if porcelain is None else bool(porcelain),
        ahead=ahead,
        behind=behind,
        last_commit=_git(path, "log", "-1", "--format=%cI"),
    )


def clean_worktree_head(descriptor: ProjectDescriptor) -> str:
    """Return the HEAD commit SHA when the worktree is clean; else empty.

    The empty string means "no stable identity": a dirty worktree, a
    non-git directory, or any probe failure. Callers use this to decide
    whether a cached check result can stand in for a fresh run — an empty
    identity never matches, so those projects always re-run.

    Args:
        descriptor: The project to identify.

    Returns:
        The 40-char SHA for a clean checkout, or ``""``.
    """
    head = _git(descriptor.path, "rev-parse", "HEAD")
    porcelain = _git(descriptor.path, "status", "--porcelain")
    if head is None or porcelain is None or porcelain:
        return ""
    return head

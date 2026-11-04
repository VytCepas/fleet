"""The approval gate keys its rows on the repository's GitHub slug.

`.agents/ship.conf` names the repo and `.agents/required-checks.tsv` carries one row per
required check, keyed `<owner>/<repo>`. The gate matches a PR's repo against that column
exactly, so after a repo rename a stale slug means "the gate lists no required check for
this repo" and only branch protection can gate. Both files must name the slug the
checkout's origin names.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]


def _origin_slug() -> str | None:
    try:
        url = subprocess.run(
            ["git", "-C", str(_ROOT), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def _rows() -> list[str]:
    path = _ROOT / ".agents" / "required-checks.tsv"
    return [
        line.split("\t")[0]
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _ship_repo() -> str:
    for line in (_ROOT / ".agents" / "ship.conf").read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "repo":
            return parts[1]
    raise AssertionError("ship.conf has no `repo` line")


@pytest.fixture(scope="module")
def slug() -> str:
    found = _origin_slug()
    if found is None:
        pytest.skip("no github.com origin to compare the gate rows against")
    return found


def test_ship_conf_names_the_origin_repo(slug: str) -> None:
    assert _ship_repo().lower() == slug.lower()


def test_required_check_rows_are_keyed_on_the_origin_repo(slug: str) -> None:
    rows = _rows()
    assert rows, "required-checks.tsv declares nothing"
    assert {r.lower() for r in rows} == {slug.lower()}

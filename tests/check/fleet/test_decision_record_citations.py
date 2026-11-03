"""Every ADR a file cites must be findable (#189).

Four numbers — ADR-012, ADR-017, ADR-024, ADR-025 — were cited across ``src/``
and ``docs/`` as if they were this repo's decisions, and no file for them exists
in ``.agents/docs/adr/``. They are project-init's ADRs: the descriptor contract,
the tier model and the credential boundary are decided upstream, and this repo
consumes them. A bare ``ADR-025 §4`` read as a local section of a local document
that had no first section, and an audit spent effort looking for it.

So a citation takes one of two forms, and this test enforces it:

- ``ADR-NNN`` — resolves to ``.agents/docs/adr/adr-NNN-*.md`` in this repo;
- ``project-init ADR-NNN`` — resolves to a row of ``.agents/docs/adr/UPSTREAM.md``,
  which names the upstream file. Accepting any qualified number would let a typo
  point at nothing, so the qualifier alone is not enough.

Files project-init renders cite project-init's numbering in project-init's own
words, so that text is fixed upstream, not here. For the scaffold-managed set,
the upgrade base records what project-init rendered. Only lines absent from that
base, the local edits a merge-managed file keeps, are scanned. Both the set and
its base are READ from ``.upgrade-base.json``: a hand-written list of managed
files would be a second copy of a fact the scaffold already records.

The descriptor (``.agents/config.yaml``) has no recorded base and is safe to
hand-edit, so it is scanned too. A comment there whose text matches a comment in
the golden project-init descriptors is project-init's and is blanked; anything
else, a hand-written note or an edited comment, is checked.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_ADR_DIR = _ROOT / ".agents" / "docs" / "adr"
_INDEX = ".agents/docs/adr/UPSTREAM.md"

#: The whole token that starts with a digit, not exactly three digits: a dropped
#: or doubled digit (``ADR-03``, ``ADR-0250``) or a trailing character
#: (``ADR-025O``) must be reported, not silently read as a valid number. The
#: ``ADR-NNN`` placeholder starts with a letter and is not a citation.
_CITATION = re.compile(r"ADR-(\d\w*)")
_UPSTREAM = "project-init "
_INDEX_ROW = re.compile(
    r"^\| project-init ADR-(\d{3}) \| `adr-(\d{3})-[a-z0-9-]+\.md` \|", flags=re.MULTILINE
)

#: Tracked paths not scanned, each with the reason. Scaffold-managed files are
#: never listed: they are scanned for their local edits (see ``local_delta``).
_EXEMPT: dict[str, str] = {
    ".agents/.upgrade-base.json": "verbatim bodies of project-init's managed files",
    ".claude/.upgrade-base.json": "verbatim bodies of project-init's managed files",
    "fixtures/deploy/scaffolder/config-v1.yaml": "golden project-init output, not authored here",
    "fixtures/deploy/scaffolder/config-v2.yaml": "golden project-init output, not authored here",
    "fixtures/deploy/scaffolder/capabilities-v2.md": "golden project-init output, not authored here",
    "fixtures/deploy/scaffolder/schemas/descriptor.schema.json": "vendored project-init schema",
    "tests/check/fleet/test_decision_record_citations.py": "this file: its docstring and cases name bare numbers on purpose",
}

#: A verbatim copy of the naming standard's package, held byte-for-byte by its MANIFEST (`just docs-check`).
_VENDORED_PREFIX = "vendor/naming/"

_UPGRADE_BASES = (".agents/.upgrade-base.json", ".claude/.upgrade-base.json")
_PROJECTION = ".claude/.projection.json"
_DESCRIPTOR = ".agents/config.yaml"
_GOLDEN_DESCRIPTORS = (
    "fixtures/deploy/scaffolder/config-v1.yaml",
    "fixtures/deploy/scaffolder/config-v2.yaml",
)


def citations(text: str) -> list[tuple[int, str, bool]]:
    """``(line, number, qualified)`` for every ADR citation in ``text``."""
    return [
        (
            text.count("\n", 0, match.start()) + 1,
            match.group(1),
            text[: match.start()].endswith(_UPSTREAM),
        )
        for match in _CITATION.finditer(text)
    ]


def phantoms(text: str, local: set[str], upstream: set[str]) -> list[tuple[int, str]]:
    """``(line, citation)`` for each citation that resolves to nothing."""
    return [
        (line, f"{_UPSTREAM if qualified else ''}ADR-{number}")
        for line, number, qualified in citations(text)
        if number not in (upstream if qualified else local)
    ]


def upstream_index(text: str) -> set[str]:
    """The numbers ``UPSTREAM.md`` records, each row naming its own file."""
    rows = _INDEX_ROW.findall(text)
    mismatched = [(cited, filed) for cited, filed in rows if cited != filed]
    assert not mismatched, f"UPSTREAM.md rows naming another ADR's file: {mismatched}"
    return {cited for cited, _ in rows}


def _tracked() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=_ROOT, capture_output=True, text=True, check=False
    )
    return [p for p in result.stdout.split("\0") if p] if result.returncode == 0 else []


def _local_adrs() -> set[str]:
    return {
        m.group(1) for p in _ADR_DIR.glob("adr-*.md") if (m := re.match(r"adr-(\d{3})-", p.name))
    }


def _managed() -> dict[str, set[str]]:
    """Scaffold-managed path -> every line project-init rendered for it."""
    managed: dict[str, set[str]] = {}
    for base in _UPGRADE_BASES:
        for path, body in json.loads((_ROOT / base).read_text(encoding="utf-8")).items():
            managed.setdefault(path, set()).update(body.splitlines())
    # `.claude/` is project-init's projection of `.agents/`, recorded in
    # `.projection.json`: a projected copy carries its source's rendered lines.
    projection = json.loads((_ROOT / _PROJECTION).read_text(encoding="utf-8"))
    for rel in projection["paths"]:
        if (source := f".agents/{rel}") in managed:
            managed.setdefault(f".claude/{rel}", set()).update(managed[source])
    return managed


def local_delta(text: str, rendered: set[str]) -> str:
    """``text`` with every line project-init rendered blanked; line numbers kept."""
    return "\n".join("" if line in rendered else line for line in text.splitlines())


def _comment(line: str) -> str | None:
    return line.split("#", 1)[1].strip() if "#" in line else None


def _rendered_comments() -> set[str]:
    """Every comment text the golden project-init descriptors carry."""
    comments = set()
    for golden in _GOLDEN_DESCRIPTORS:
        for line in (_ROOT / golden).read_text(encoding="utf-8").splitlines():
            if (comment := _comment(line)) is not None:
                comments.add(comment)
    return comments


def descriptor_delta(text: str, rendered: set[str]) -> str:
    """``text`` with each project-init comment cut off its line; line numbers kept."""
    return "\n".join(
        line.split("#", 1)[0] if _comment(line) in rendered else line for line in text.splitlines()
    )


@pytest.fixture(scope="module")
def scanned() -> dict[str, str]:
    """Path -> text for every tracked, authored-here text file."""
    # mutmut runs the suite from a copy of the tree with no ADR directory and no
    # git index of its own; there is nothing to scan there, so say so.
    files = _tracked()
    if not _ADR_DIR.is_dir() or not files:
        pytest.skip("not a git checkout of this repo (e.g. mutmut's mutants/ copy)")
    stale = sorted(set(_EXEMPT) - set(files))
    assert not stale, f"exemptions for untracked paths: {stale}"
    managed = _managed()
    texts = {}
    for path in files:
        if path in _EXEMPT or path.startswith(_VENDORED_PREFIX):
            continue
        try:
            text = (_ROOT / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        if path in managed:
            text = local_delta(text, managed[path])
        elif path == _DESCRIPTOR:
            text = descriptor_delta(text, _rendered_comments())
        texts[path] = text
    assert len(texts) > 50, f"scanned only {len(texts)} files — the listing is broken, not clean"
    return texts


def test_every_cited_adr_resolves_here_or_upstream(scanned: dict[str, str]) -> None:
    local = _local_adrs()
    upstream = upstream_index(scanned[_INDEX])
    bad = [
        f"{path}:{line}: {citation}"
        for path, text in scanned.items()
        for line, citation in phantoms(text, local, upstream)
    ]
    assert not bad, (
        "ADR cited that resolves to nothing. Write the ADR in .agents/docs/adr/, or, if "
        f"the decision is project-init's, cite it as 'project-init ADR-NNN' with a row in "
        f"{_INDEX}:\n  " + "\n  ".join(bad)
    )


def test_every_upstream_row_is_cited(scanned: dict[str, str]) -> None:
    # A row nothing cites is an index entry nobody re-reads.
    upstream = upstream_index(scanned[_INDEX])
    cited = {
        number
        for path, text in scanned.items()
        if path != _INDEX
        for _, number, qualified in citations(text)
        if qualified
    }
    assert upstream, "UPSTREAM.md has no rows — the row pattern no longer matches the table"
    assert not upstream - cited, f"UPSTREAM.md rows cited nowhere: {sorted(upstream - cited)}"


def test_the_scan_sees_local_citations(scanned: dict[str, str]) -> None:
    # Control: a scanner that matched nothing would pass the tests above on any tree.
    local = _local_adrs()
    assert {"003", "006", "007"} <= local
    cited = {
        number
        for path, text in scanned.items()
        if path.startswith("src/")
        for _, number, _ in citations(text)
    }
    assert {"003", "006", "007"} <= cited


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see ADR-003 for the engine", []),
        ("per ADR-025 §4, degrade by tier", [(1, "ADR-025")]),
        ("per project-init ADR-025 §4", []),
        ("per project-init ADR-251, a typo", [(1, "project-init ADR-251")]),
        ("line one\nADR-003 / ADR-012 boundary", [(2, "ADR-012")]),
        ("ADR-003 / project-init ADR-012 boundary", []),
        ("the project-init\nADR-012 wrapped across a line", [(2, "ADR-012")]),
        ("project-init ADR-003 is not this repo's ADR-003", [(1, "project-init ADR-003")]),
        ("per project-init ADR-0250, a doubled digit", [(1, "project-init ADR-0250")]),
        ("see ADR-03, a dropped digit", [(1, "ADR-03")]),
        ("per project-init ADR-025O, a trailing letter", [(1, "project-init ADR-025O")]),
        ("cite it as ADR-NNN, the placeholder", []),
    ],
)
def test_phantoms(text: str, expected: list[tuple[int, str]]) -> None:
    assert phantoms(text, {"003"}, {"012", "025"}) == expected


def test_an_index_row_must_name_its_own_file() -> None:
    row = "| project-init ADR-012 | `adr-017-per-surface-config-generator.md` | x |\n"
    with pytest.raises(AssertionError, match="another ADR's file"):
        upstream_index(row)


def test_local_delta_keeps_only_lines_project_init_did_not_render() -> None:
    text = "rendered ADR-012 line\nlocal edit citing ADR-099\nrendered tail"
    delta = local_delta(text, {"rendered ADR-012 line", "rendered tail"})
    assert phantoms(delta, {"003"}, set()) == [(2, "ADR-099")]


def test_managed_files_are_scanned_for_their_local_edits(scanned: dict[str, str]) -> None:
    # Control for the wiring: several managed files carry local edits today, so
    # a scan that dropped managed files wholesale would leave every one blank.
    managed = _managed()
    edited = [path for path in scanned if path in managed and scanned[path].strip()]
    assert edited, "no managed file has a scanned local edit — managed files are being skipped"


def test_descriptor_delta_keeps_hand_written_comments() -> None:
    text = "a: 1  # plugin payload version (ADR-010)\nb: 2  # local note, see ADR-099\n"
    delta = descriptor_delta(text, {"plugin payload version (ADR-010)"})
    assert phantoms(delta, {"003"}, set()) == [(2, "ADR-099")]


def test_the_descriptor_reaches_the_scan(scanned: dict[str, str]) -> None:
    # Control for the wiring: its keys are hand-editable and always scanned.
    assert "memory:" in scanned[_DESCRIPTOR]

"""The seams freshness check (box register, seams): a copy one commit behind its producer is red.

Each case builds throwaway git repos for a producer and a consumer, so the red is
the real comparison, not a stub: the producer moves one commit and the copy does not.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from projects_orchestrator import seams

_REGISTER = (
    "# seams\n"
    "id\tkind\tproducer\tconsumer\tcopy_path\tpin\tschema\tfreshness\towner\tallowed_lag\treason\n"
    "pkg\tvendor\tprod\tcons\tvendor/pkg/\tMANIFEST.tsv#src_commit\t-\tcheck.seam-fresh\tstandard\t{lag}\t-\n"
    "sch\tcontract\tprod\tcons\tfixtures/schemas/a.schema.json\t-\tschemas/a.schema.json\tcheck.seam-fresh\tscope\t0\t-\n"
)


def _git(root: Path, *args: str) -> str:
    """Run git in ``root`` and return stdout."""
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(root: Path, message: str) -> str:
    """Commit everything and return the new head."""
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _write(path: Path, text: str) -> None:
    """Write ``text`` at ``path``, making its folder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _world(tmp_path: Path, lag: int = 0) -> tuple[dict[str, Path], Path, str]:
    """A producer and a consumer holding a faithful copy of it; returns roots, register, pin."""
    prod, cons = tmp_path / "prod", tmp_path / "cons"
    for root in (prod, cons):
        root.mkdir()
        _git(root, "init", "-q")
    _write(prod / "std/a.tsv", "id\none\n")
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    _write(prod / "schemas/a.schema.json", json.dumps(schema))
    pin = _commit(prod, "first")
    sha = hashlib.sha256(b"id\none\n").hexdigest()
    _write(cons / "vendor/pkg/a.tsv", "id\none\n")
    _write(cons / "vendor/pkg/MANIFEST.tsv", f"# src_commit: {pin}\n{'a.tsv'}\t{sha}\tstd/a.tsv\n")
    _write(cons / "fixtures/schemas/a.schema.json", json.dumps(schema))
    _commit(cons, "vendored")
    register = tmp_path / "seams.tsv"
    register.write_text(_REGISTER.format(lag=lag), encoding="utf-8")
    return {"prod": prod, "cons": cons}, register, pin


def _run(roots: dict[str, Path], register: Path) -> list[seams.Finding]:
    """The check over a world, with HEAD as the producer ref."""
    rows = seams.load_register(register.read_text("utf-8"))
    refs = {name: "HEAD" for name in roots}
    tracked = {"cons": seams._tracked(roots["cons"])}
    return seams.check(rows, roots, seams.git_reader(roots), refs, tracked)


def test_a_faithful_copy_is_fresh(tmp_path: Path) -> None:
    """Nothing differs, so nothing is reported."""
    roots, register, _ = _world(tmp_path)
    assert _run(roots, register) == []


def test_a_copy_one_commit_behind_is_stale(tmp_path: Path) -> None:
    """The producer moves one commit; the copy does not; the vendored tree is red."""
    roots, register, _ = _world(tmp_path)
    _write(roots["prod"] / "std/a.tsv", "id\none\ntwo\n")
    _commit(roots["prod"], "second")
    found = _run(roots, register)
    assert [(f.seam, f.kind) for f in found] == [("pkg", "stale")]
    assert "a.tsv" in found[0].detail
    assert "1 commit(s) behind" in found[0].detail


def test_an_allowed_lag_holds_the_finding_back(tmp_path: Path) -> None:
    """One commit behind with a lag of one allowed is a pin, not a finding."""
    roots, register, _ = _world(tmp_path, lag=1)
    _write(roots["prod"] / "std/a.tsv", "id\none\ntwo\n")
    _commit(roots["prod"], "second")
    assert _run(roots, register) == []


def test_a_producer_commit_that_leaves_the_copy_alone_is_fresh(tmp_path: Path) -> None:
    """A commit elsewhere in the producer moves no file the copy holds."""
    roots, register, _ = _world(tmp_path)
    _write(roots["prod"] / "elsewhere.txt", "x")
    _commit(roots["prod"], "unrelated")
    assert _run(roots, register) == []


def test_a_retyped_schema_field_is_stale(tmp_path: Path) -> None:
    """The contract copy differs in what constrains data, so it is red; wording alone would not be."""
    roots, register, _ = _world(tmp_path)
    _write(
        roots["prod"] / "schemas/a.schema.json",
        json.dumps({"type": "object", "properties": {"x": {"type": "integer"}}}),
    )
    _commit(roots["prod"], "retype")
    assert [(f.seam, f.kind) for f in _run(roots, register)] == [("sch", "stale")]


def test_a_reworded_schema_is_not_stale(tmp_path: Path) -> None:
    """A changed description constrains nothing, so the copy is still fresh."""
    roots, register, _ = _world(tmp_path)
    schema = {"type": "object", "description": "reworded", "properties": {"x": {"type": "string"}}}
    _write(roots["prod"] / "schemas/a.schema.json", json.dumps(schema))
    _commit(roots["prod"], "reword")
    assert _run(roots, register) == []


def test_a_copy_with_no_row_is_unregistered(tmp_path: Path) -> None:
    """A MANIFEST.tsv no row names is red, and so is a fixture schema no row names."""
    roots, register, _ = _world(tmp_path)
    _write(roots["cons"] / "other/vendor/MANIFEST.tsv", "# src_commit: x\n")
    _write(roots["cons"] / "fixtures/schemas/b.schema.json", "{}")
    _commit(roots["cons"], "stray")
    found = sorted(f.seam for f in _run(roots, register) if f.kind == "unregistered")
    assert found == ["copy:fixtures/schemas/b.schema.json", "copy:other/vendor/"]


def test_a_row_whose_copy_is_gone_is_missing(tmp_path: Path) -> None:
    """The row stays and the copy goes: red the other way round."""
    roots, register, _ = _world(tmp_path)
    (roots["cons"] / "fixtures/schemas/a.schema.json").unlink()
    assert ("sch", "missing-copy") in [(f.seam, f.kind) for f in _run(roots, register)]


def test_a_missing_checkout_is_unchecked_never_green(tmp_path: Path) -> None:
    """No producer checkout means the row was not judged; the exit says so."""
    roots, register, _ = _world(tmp_path)
    del roots["prod"]
    rows = seams.load_register(register.read_text("utf-8"))
    found = seams.check(rows, roots, seams.git_reader(roots), {"cons": "HEAD"}, {})
    assert {f.kind for f in found} == {"unchecked"}


def test_the_command_exits_one_on_a_stale_copy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The scheduled job reads the exit code: 1 for a finding."""
    roots, register, _ = _world(tmp_path)
    _write(roots["prod"] / "std/a.tsv", "id\nchanged\n")
    _commit(roots["prod"], "second")
    code = seams.main(
        [
            "--register",
            str(register),
            "--root",
            f"prod={roots['prod']}",
            "--root",
            f"cons={roots['cons']}",
            "--ref",
            "HEAD",
        ]
    )
    assert code == 1
    assert capsys.readouterr().out.startswith("pkg\tstale\t")

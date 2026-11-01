"""Compose every passive governance probe into one fleet audit.

``audit`` answers "does anything in this project need attention?" in one
report and one exit code. It adds no new probe beyond a memory-schema lint:
it composes :func:`doctor.diagnose` (contract conformance), scaffold-drift
*divergence* (``doctor`` only checks that a manifest exists), and check
freshness — plus a memory pass that ports ``lint_memory.sh``'s intent into the
never-raise engine, and a verbs pass that asks the naming lint whether the
justfile carries the task verbs the project's repo-kind requires (harbor#1428).

``doctor`` stays the focused contract-conformance command; ``audit`` is the
broad governance report that reuses it. Every finding is ``ok | warn | fail``;
a report's status is its worst finding and the CLI exits non-zero when
anything is ``warn`` or worse.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.doctor import FAIL, OK, WARN, diagnose
from projects_orchestrator.drift import compute_drift
from projects_orchestrator.memory import load_project_memory

_log = logging.getLogger(__name__)

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_REPO_KIND_RE = re.compile(r"^repo-kind:[ \t]*[\"']?([a-z][a-z0-9-]*)", re.MULTILINE)
_VERB_TIMEOUT_S = 60


@dataclass(frozen=True)
class AuditFinding:
    """One governance finding for one project.

    Attributes:
        project: Project name.
        category: Probe name (``config``/``contract``/…/``drift``/``memory``/
            ``freshness``/``verbs``).
        severity: ``ok`` | ``warn`` | ``fail``.
        message: Short human-readable detail.
    """

    project: str
    category: str
    severity: str
    message: str


@dataclass(frozen=True)
class AuditReport:
    """All governance findings for one project.

    Attributes:
        project: Project name.
        findings: Findings in probe order.
    """

    project: str
    findings: tuple[AuditFinding, ...] = ()

    @property
    def status(self) -> str:
        """Worst finding severity across the report (``fail`` > ``warn`` > ``ok``)."""
        severities = {finding.severity for finding in self.findings}
        if FAIL in severities:
            return FAIL
        if WARN in severities:
            return WARN
        return OK

    @property
    def needs_attention(self) -> bool:
        """Whether any finding is ``warn`` or worse."""
        return self.status in (WARN, FAIL)


def _is_empty_template(body: str) -> bool:
    """Return whether a memory body is only comments/whitespace (an unfilled stub)."""
    return not _COMMENT_RE.sub("", body).strip()


def _conformance_findings(descriptor: ProjectDescriptor) -> list[AuditFinding]:
    """Map ``doctor``'s contract-conformance checks into audit findings."""
    return [
        AuditFinding(descriptor.name, finding.check, finding.status, finding.detail)
        for finding in diagnose(descriptor).findings
    ]


def _drift_finding(descriptor: ProjectDescriptor) -> AuditFinding | None:
    """Report actual scaffold divergence; ``None`` when clean or manifest-less."""
    report = compute_drift(descriptor)
    if report.status != "drift":
        return None
    changed = len(report.modified) + len(report.missing)
    return AuditFinding(descriptor.name, "drift", WARN, f"{changed} file(s) diverged from scaffold")


def _index_mentions(index_text: str, filename: str) -> bool:
    """Whether the index references ``filename`` on a filename boundary.

    A plain substring test reports ``a.md`` as indexed whenever ``data.md`` is
    listed; anchoring on a non-name character (or start/end) avoids that.
    """
    return bool(re.search(rf"(?<![\w.-]){re.escape(filename)}(?![\w.-])", index_text))


def _read_index(memory_path: Path) -> str | None:
    """Read ``MEMORY.md`` text for index checks; ``None`` when unreadable."""
    try:
        return (memory_path / "MEMORY.md").read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _log.debug("cannot read %s: %r", memory_path / "MEMORY.md", exc)
        return None


def _memory_findings(descriptor: ProjectDescriptor) -> list[AuditFinding]:
    """Lint the project's memory: directory, index, frontmatter, empty templates."""
    memory = load_project_memory(descriptor)
    name = descriptor.name
    if memory.memory_path is None or not memory.memory_path.is_dir():
        return [AuditFinding(name, "memory", WARN, "no memory directory")]

    findings = [AuditFinding(name, "memory", WARN, warning) for warning in memory.warnings]
    if not memory.index_present:
        findings.append(AuditFinding(name, "memory", WARN, "no MEMORY.md index"))
    index_text = _read_index(memory.memory_path)
    for mem in memory.files:
        if mem.type == "unknown":
            findings.append(
                AuditFinding(name, "memory", WARN, f"{mem.path.name}: no frontmatter type")
            )
        if _is_empty_template(mem.body):
            findings.append(AuditFinding(name, "memory", WARN, f"{mem.path.name}: empty template"))
        if index_text is not None and not _index_mentions(index_text, mem.path.name):
            findings.append(AuditFinding(name, "memory", WARN, f"{mem.path.name}: not indexed"))
    if not findings:
        findings.append(
            AuditFinding(name, "memory", OK, f"{len(memory.files)} fact file(s), indexed")
        )
    return findings


def _repo_kind(root: Path) -> str | None:
    """The ``repo-kind:`` a project declares in ``.agents/config.yaml``, else ``None``."""
    try:
        text = (root / ".agents" / "config.yaml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _REPO_KIND_RE.search(text)
    return match.group(1) if match else None


def _naming_judge(root: Path) -> tuple[Path, Path] | None:
    """The naming lint and registry to judge ``root`` with, or ``None``.

    The repo's own vendored package first (what a scaffolded repo runs offline),
    else the checkout named by ``NAMING_STANDARD_REPO`` (what this repo's own
    naming gate uses).
    """
    vendored = root / "vendor" / "naming"
    if (vendored / "lint-name.sh").is_file() and (vendored / "registry").is_dir():
        return vendored / "lint-name.sh", vendored / "registry"
    standard = os.environ.get("NAMING_STANDARD_REPO")
    if standard:
        lint = Path(standard) / "bin" / "lint-name.sh"
        registry = Path(standard) / "standards" / "naming"
        if lint.is_file() and registry.is_dir():
            return lint, registry
    return None


def _verbs_findings(descriptor: ProjectDescriptor) -> list[AuditFinding]:
    """A verb the project's kind requires of its justfile and it lacks (harbor#1428).

    Calls the naming lint's ``--check repo-kind`` and keeps its ``name.repo-verb``
    records, so the verb register and the recipe reading stay the lint's. A
    project that declares no ``repo-kind`` or has no lint to judge with is
    ``ok`` with the reason: not judged is never a failure and never a pass.
    """
    name = descriptor.name
    kind = _repo_kind(descriptor.path)
    if kind is None:
        return [AuditFinding(name, "verbs", OK, "no repo-kind declared: verbs not judged")]
    judge = _naming_judge(descriptor.path)
    if judge is None:
        return [
            AuditFinding(
                name, "verbs", WARN, "unchecked: no vendor/naming/ and no NAMING_STANDARD_REPO"
            )
        ]
    lint, registry = judge
    argv = ["sh", str(lint), "--root", str(descriptor.path), "--registry", str(registry)]
    argv += ["--check", "repo-kind", ".", "--format", "findings"]
    try:
        done = subprocess.run(  # noqa: S603 - argv list, no shell, paths from the registry
            argv, capture_output=True, text=True, timeout=_VERB_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [
            AuditFinding(name, "verbs", WARN, f"unchecked: the naming lint did not run ({exc!r})")
        ]
    if done.returncode > 1:
        return [
            AuditFinding(
                name, "verbs", WARN, f"unchecked: the naming lint exited {done.returncode}"
            )
        ]
    missing = sorted(
        fields[2].removeprefix("repo-verb:")
        for fields in (line.split("\t") for line in done.stdout.splitlines())
        if len(fields) >= 3 and fields[0] in ("A", "V") and fields[1] == "name.repo-verb"
    )
    if not missing:
        return [AuditFinding(name, "verbs", OK, f"carries the verbs a {kind} repo requires")]
    return [
        AuditFinding(name, "verbs", WARN, f"missing verb: {verb} (a {kind} repo requires it)")
        for verb in missing
    ]


def _freshness_finding(name: str, cached: dict[str, CheckResult] | None) -> AuditFinding:
    """Report whether the project has any cached check results."""
    if cached:
        return AuditFinding(name, "freshness", OK, f"{len(cached)} cached result(s)")
    return AuditFinding(name, "freshness", WARN, "never checked")


def audit_project(
    descriptor: ProjectDescriptor, cached: dict[str, CheckResult] | None = None
) -> AuditReport:
    """Run every governance probe for one project (never raises).

    Args:
        descriptor: The project to audit.
        cached: Last-known check results for the project, if any.

    Returns:
        The composed audit report.
    """
    findings = _conformance_findings(descriptor)
    drift = _drift_finding(descriptor)
    if drift is not None:
        findings.append(drift)
    findings.extend(_memory_findings(descriptor))
    findings.extend(_verbs_findings(descriptor))
    findings.append(_freshness_finding(descriptor.name, cached))
    return AuditReport(project=descriptor.name, findings=tuple(findings))


def render_markdown(reports: list[AuditReport]) -> str:
    """Render audit reports as a Markdown document for a scheduled run.

    Args:
        reports: One report per project.

    Returns:
        A Markdown string; a friendly line when the fleet is empty.
    """
    if not reports:
        return "# Fleet audit\n\nNo projects discovered.\n"
    lines = ["# Fleet audit", ""]
    for report in reports:
        lines.append(f"## {report.project} — {report.status}")
        lines.append("")
        lines.append("| category | severity | detail |")
        lines.append("| --- | --- | --- |")
        lines.extend(
            f"| {finding.category} | {finding.severity} | {finding.message} |"
            for finding in report.findings
        )
        lines.append("")
    return "\n".join(lines)

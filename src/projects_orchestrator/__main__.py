"""CLI entry point for `projects-orchestrator`.

One command surface over the fleet engine: discover projects, show git
health, run their declared gates, search their memories, or drive it all
interactively (``controller`` REPL / ``tui``). Every data command takes
``--json`` so external monitors can consume the same truth the tables show.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import datetime as _dt
import json
import logging
import math
import os
import platform
import sys
import traceback
from collections.abc import Iterator
from dataclasses import asdict, replace
from pathlib import Path

from projects_orchestrator import (
    __version__,
    cache,
    campaign,
    cost,
    models,
    orphans,
    runs,
    selector,
    watchdog,
    work,
)
from projects_orchestrator.adapters import gcp
from projects_orchestrator.adapters.cloud import (
    DEPLOY_ACTIONS,
    DISPATCH_DISPATCHED,
    DISPATCH_FAILED,
    SETTLE_SUCCEEDED,
    DeployDispatch,
    DeploySettlement,
    collect_cloud,
    latest_run_id,
    resolved_workflow,
    trigger_deploy,
    wait_for_deploy,
)
from projects_orchestrator.adapters.cloud import as_check_results as cloud_check_results
from projects_orchestrator.adapters.forge import probe_ci
from projects_orchestrator.adapters.gitlab import provider_is_gitlab
from projects_orchestrator.adapters.project_init import (
    SCAFFOLD_JSON_COMMAND,
    latest_plugin_version,
    latest_upstream_version,
    parse_scaffold_result,
    trigger_upgrade,
)
from projects_orchestrator.audit import AuditReport, audit_project, render_markdown
from projects_orchestrator.capabilities import (
    HOOK,
    MCP,
    SKILL,
    load_capabilities,
)
from projects_orchestrator.capabilities import (
    aggregate as aggregate_capabilities,
)
from projects_orchestrator.checks import DEFAULT_TASKS, CheckResult, collect_checks
from projects_orchestrator.controller import ControllerContext, dispatch, parse_command
from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.digest import (
    compute_digest,
    digest_payload,
    load_prior,
    render_digest,
    save_current,
)
from projects_orchestrator.doctor import diagnose
from projects_orchestrator.drift import compute_drift
from projects_orchestrator.fleet import ProjectSnapshot, fleet_rows, fleet_snapshots, render_table
from projects_orchestrator.hardening import checklist as hardening_checklist
from projects_orchestrator.hardening import render_text as render_hardening
from projects_orchestrator.heal import (
    HEAL_MODES,
    HEALABLE_TASKS,
    MODE_FIX,
    FleetHealReport,
    heal_fleet,
    render_fleet_heal_report,
)
from projects_orchestrator.heal_issues import heal_issue_sink
from projects_orchestrator.history import DEFAULT_TREND_WIDTH as HISTORY_TREND_WIDTH
from projects_orchestrator.history import load_history, project_history, sparkline, transitions
from projects_orchestrator.history import record as history_record
from projects_orchestrator.host import host_health
from projects_orchestrator.html import render_html
from projects_orchestrator.memory import (
    load_memory,
    load_memory_sources,
    retrieval_mode,
    search_memory,
)
from projects_orchestrator.notify import (
    alerts_payload,
    fleet_alerts,
    heal_webhook_sink,
    post_payload,
    post_webhook,
    render_alerts,
)
from projects_orchestrator.observability import filter_since, load_events
from projects_orchestrator.pool import map_ordered
from projects_orchestrator.registry import (
    REGISTRY_FILENAME,
    ROOT_ENV_NAME,
    SCAN_ROOT_ERROR,
    Fleet,
    FleetConfig,
    default_fleet_config,
    discover,
    load_fleet_config,
    register_project,
)
from projects_orchestrator.server import DEFAULT_HOST, DEFAULT_PORT, is_loopback, serve
from projects_orchestrator.status import clean_worktree_head, collect_status
from projects_orchestrator.supervisor import liveness_check
from projects_orchestrator.supervisor import logs as run_logs
from projects_orchestrator.supervisor import start as run_start
from projects_orchestrator.supervisor import stop as run_stop
from projects_orchestrator.testfleet import CHECK_ID as FLEET_TEST_ID
from projects_orchestrator.testfleet import DEFAULT_TIMEOUT as FLEET_TEST_TIMEOUT
from projects_orchestrator.testfleet import STAMP as FLEET_TEST_STAMP
from projects_orchestrator.testfleet import exit_code as fleet_test_exit
from projects_orchestrator.testfleet import render as render_fleet_test
from projects_orchestrator.testfleet import run_fleet
from projects_orchestrator.upgrade import UpgradeRow, upgrade_plan

_log = logging.getLogger(__name__)

#: Truthy value turns on the diagnostic trail without the flag, for a scheduled
#: run whose command line is harder to change than its environment.
VERBOSE_ENV = "PROJECTS_ORCHESTRATOR_VERBOSE"
#: sysexits EX_SOFTWARE: the orchestrator itself failed, which is not the
#: "something in the fleet needs attention" that 1 means on every data command.
EXIT_INTERNAL_ERROR = 70
EXIT_UNSUPPORTED_PLATFORM = 2
_NATIVE_WINDOWS_PREFIXES = ("MINGW", "MSYS", "CYGWIN", "Windows")
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _fleet_config(args: argparse.Namespace) -> FleetConfig:
    """Resolve discovery config from --fleet / --root / defaults.

    Also activates the config's model table (:mod:`models`): every CLI path that
    can launch an agent resolves its config here, so this is where the fleet
    file's ``models:`` becomes the table the launchers read.
    """
    if args.fleet:
        config = load_fleet_config(Path(args.fleet))
    elif args.root:
        config = FleetConfig(roots=(Path(args.root).expanduser().resolve(),))
    else:
        config = default_fleet_config()
    models.configure(config.models)
    return config


def _discover(args: argparse.Namespace) -> Fleet:
    """Discover the fleet, surfacing warnings on stderr.

    An empty result says WHERE IT LOOKED (#204). Every command but `watch`
    reports an unresolved fleet as "no projects discovered" and exit 0 — and
    `doctor`, `drift` and `audit` print nothing at all — which is
    indistinguishable from a fully conformant fleet. The exit codes are left
    alone here deliberately (see below); what changes is that the operator is
    told which roots were scanned and which config, if any, was read, so a
    misconfiguration stops looking like a clean bill of health.

    NOT AN EXIT-CODE CHANGE, and that is not an oversight. `watch` exits 2 on an
    empty fleet and the same reasoning covers the reporting commands, but this
    helper is shared by all 20 discovery call sites — including `register`,
    where an empty fleet is the NORMAL first-run state. Failing here would break
    the first registration on a new machine. Doing it properly means opting the
    reporting commands in one at a time, which is its own change.
    """
    fleet = discover(_fleet_config(args))
    for warning in fleet.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if not fleet.descriptors:
        config = fleet.config
        if config.source is not None:
            where = f"fleet file {config.source}"
        elif config.roots:
            where = "scanned " + ", ".join(str(r) for r in config.roots)
        else:
            where = "no roots configured"
        print(
            f"warning: no projects discovered ({where}). A project needs"
            " .agents/config.yaml (or a legacy .claude/ one). Point discovery"
            f" with --root/--fleet, or export {ROOT_ENV_NAME}.",
            file=sys.stderr,
        )
    return fleet


#: Exit code a REPORTING command returns when discovery resolved to nothing.
#: ``watch`` has used it since before #204; these commands now match it.
NO_PROJECTS_RC = 2


def _unresolved_rc(fleet: Fleet, ok: int = 0) -> int:
    """Fail a reporting command whose fleet resolved to nothing (#204).

    An unresolved fleet used to be byte-identical to a conformant one: empty
    output and exit 0. ``doctor`` was the worst of them, because it is the
    command you run TO FIND OUT whether the fleet is set up — silence plus
    success is exactly what a fully conformant fleet looks like.

    OPTED IN PER COMMAND, DELIBERATELY, and this is the shape :func:`_discover`
    said the fix had to take. That helper is shared by all 20 discovery call
    sites including ``register``, where an empty fleet is the NORMAL first-run
    state and failing would break the first registration on a new machine. So
    the check lives here and the reporting commands call it; the write paths do
    not.

    Args:
        fleet: The discovered fleet.
        ok: The exit code to use when the fleet resolved to something — so a
            command that already fails for its own reasons keeps that code.

    Returns:
        ``NO_PROJECTS_RC`` on an empty fleet, else ``ok``.
    """
    return NO_PROJECTS_RC if not fleet.descriptors else ok


def _print_watch_state() -> None:
    """Tell the operator when the scheduled pass has stopped firing (#186).

    Printed by the surfaces a human reads when watch is NOT running — the pass
    itself cannot report its own death. Only a fault speaks: a fresh watch, and
    a watch whose interval was never declared, stay quiet, because an advisory
    on every ordinary `status` is the noise that trains people to skim.
    """
    state = watchdog.read_state()
    if state.needs_attention:
        print(f"warning: {watchdog.describe(state)}", file=sys.stderr)


def _emit_json(payload: object) -> int:
    """Print a JSON document (paths become strings).

    Returns 0, which is the exit code only for a command whose verdict is always
    0. A command whose text mode can exit nonzero must return THAT code under
    ``--json`` too (#278): a monitor can ignore an exit code, but it cannot
    recover one it was never given.
    """
    print(json.dumps(payload, indent=2, default=str))
    return 0


def _cmd_projects(args: argparse.Namespace) -> int:
    """List discovered projects."""
    fleet = _discover(args)
    if args.json:
        emitted = _emit_json([asdict(d) for d in fleet.descriptors])
        return _unresolved_rc(fleet, emitted)
    for descriptor in fleet.descriptors:
        print(f"{descriptor.name}  ({descriptor.language}, {descriptor.path})")
    if not fleet.descriptors:
        print("no projects discovered")
    return _unresolved_rc(fleet)


def _cmd_status(args: argparse.Namespace) -> int:
    """Show the fleet table, or one project's git health."""
    fleet = _discover(args)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        status = collect_status(descriptor)
        # Every status mode, not just the unfiltered table: a human drilling
        # into one project, and a monitor reading --json, were both told
        # nothing when the timer had stopped (raised in review on #244). It
        # goes to stderr so the JSON document on stdout stays exactly the
        # array/object its consumers parse.
        _print_watch_state()
        if args.json:
            return _emit_json(asdict(status))
        print(f"{status.project}: {status.health} on {status.branch or '?'}")
        return 0
    snapshots = fleet_snapshots(fleet)
    selected = _select(args, snapshots)
    if selected is None:
        return 2
    _print_watch_state()
    if args.json:
        emitted = _emit_json([asdict(s.status) for s in selected])
        return _unresolved_rc(fleet, emitted)
    if not selected:
        print("no projects match")
        return _unresolved_rc(fleet)
    # Pass the FULL fleet as the version reference so a filtered row still shows
    # "behind" when a project the filter hid is newer — not a false "=".
    print(render_table(fleet_rows(selected, snapshots)))
    # The host-health tile (#247), under the table so the header stays line 1.
    print(host_health(fleet.config.host_health_command))
    return _unresolved_rc(fleet)


def _reusable_pass(
    cached: dict[str, CheckResult] | None, task: str, head: str
) -> CheckResult | None:
    """Return the cached result that can stand in for a fresh run, if any.

    A cached result is trusted only when it is a ``pass`` recorded at the
    same clean-worktree HEAD the project is at now. Fails, skips, dirty
    trees, and unknown identities never match — they always re-run.
    """
    if not head or cached is None:
        return None
    result = cached.get(task)
    if result is not None and result.status == "pass" and result.head == head:
        return result
    return None


def _project_checks(
    descriptor: ProjectDescriptor,
    tasks: tuple[str, ...],
    cached: dict[str, CheckResult] | None,
    changed_only: bool,
) -> list[tuple[CheckResult, bool]]:
    """Run one project's gates, reusing cached passes when allowed.

    Returns:
        ``(result, reused)`` pairs in task order; ``reused`` marks results
        served from the cache instead of a fresh run.
    """
    head = clean_worktree_head(descriptor)
    reusable: dict[str, CheckResult] = {}
    if changed_only:
        reusable = {
            task: result
            for task in tasks
            if (result := _reusable_pass(cached, task, head)) is not None
        }
    to_run = tuple(task for task in tasks if task not in reusable)
    fresh_results = collect_checks(descriptor, to_run, head=head)
    # Re-verify the worktree did not change while the gates ran: a tracked file
    # edited mid-run and reverted would otherwise stamp a pass under `head` that
    # a fresh run at that same HEAD would not reproduce. On any change, drop the
    # recorded identity so --changed-only can never reuse these results.
    if head and clean_worktree_head(descriptor) != head:
        fresh_results = [replace(result, head="") for result in fresh_results]
    fresh = dict(zip(to_run, fresh_results, strict=True))
    return [(reusable[task], True) if task in reusable else (fresh[task], False) for task in tasks]


def _cmd_checks(args: argparse.Namespace) -> int:
    """Run declared gates; exit 1 when any project fails one."""
    fleet = _discover(args)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    else:
        selected = list(fleet.descriptors)

    tasks = tuple(args.task) if args.task else DEFAULT_TASKS
    cached = cache.load_results() if args.changed_only else {}
    per_project = map_ordered(
        lambda d: _project_checks(d, tasks, cached.get(d.name), args.changed_only),
        selected,
        jobs=args.jobs,
    )
    pairs = [pair for project_pairs in per_project for pair in project_pairs]
    fresh = [result for result, reused in pairs if not reused]
    cache.save_results(fresh)
    history_record(fresh)
    rc = 1 if any(result.status == "fail" for result, _ in pairs) else 0
    if args.json:
        _emit_json([{**asdict(r), "cached": reused} for r, reused in pairs])
        return rc
    for result, reused in pairs:
        suffix = f" — {result.detail}" if result.detail else ""
        cached_mark = " (cached)" if reused else ""
        print(f"{result.project} {result.task}: {result.status}{cached_mark}{suffix}")
    return rc


def _cmd_test_fleet(args: argparse.Namespace) -> int:
    """Run every repo's `just test` (the test contract); exit 1 when any repo fails (#316)."""
    # Not _discover(): every line this check prints carries its id, warnings included.
    fleet = discover(_fleet_config(args))
    # A root that could not be scanned may hide projects nobody ran `just test`
    # against: a healthy sibling root filling `selected` must never let that gap
    # read as green (#320 review).
    unscanned = [w for w in fleet.warnings if w.startswith(SCAN_ROOT_ERROR)]
    # An explicit `projects:` entry discover() dropped — missing, unreadable,
    # refused, or not a project-init project — checked structurally (configured
    # paths against admitted descriptors), never by matching a warning's text:
    # the fix above only recognized the `cannot scan root` prefix, so a dropped
    # explicit entry (warned e.g. "not a project-init project: ...") tested
    # every OTHER repo and still exited 0 (#320 review).
    admitted = {d.path.resolve() for d in fleet.descriptors}
    dropped = sorted({p.resolve() for p in fleet.config.projects} - admitted, key=str)
    for warning in fleet.warnings:
        print(f"{FLEET_TEST_STAMP} warning: {warning}", file=sys.stderr)
    for entry in dropped:
        print(f"{FLEET_TEST_STAMP} configured but not tested: {entry}", file=sys.stderr)
    selected = list(fleet.descriptors)
    if args.project:
        found = fleet.get(args.project)  # case-insensitive, like every other handler (#322)
        selected = [found] if found in selected else []
        if not selected:
            print(f"{FLEET_TEST_STAMP} unknown project: {args.project}")
            return 2
    if not selected:
        # An empty fleet is not a green one: "nothing failed" would be a vacuous pass.
        print(f"{FLEET_TEST_STAMP} no projects discovered — nothing was tested")
        return 2
    rows = run_fleet(selected, timeout=args.timeout, jobs=args.jobs)
    # Fleet-completeness gaps (unscanned, dropped) are about repos that never
    # ran at all — real for an unfiltered run, but by the time --project has
    # matched a name here, that project was admitted and scanned, so an
    # unrelated gap elsewhere must not fail it (#323).
    incomplete = not args.project and (unscanned or dropped)
    rc = 1 if (fleet_test_exit(rows) or incomplete) else 0
    if args.json:
        _emit_json({"check": FLEET_TEST_ID, "exit": rc, "rows": [asdict(row) for row in rows]})
        return rc
    for line in render_fleet_test(rows):
        print(line)
    return rc


def _cmd_memory(args: argparse.Namespace) -> int:
    """Search every project's memory, using each project's tier retrieval surface."""
    fleet = _discover(args)
    # Degrade-by-tier (project-init ADR-025 §4): grep the memory_path baseline, and add the
    # graph's facts for tier>=2 children. RAG-tier children are noted so an
    # operator knows a surface exists that this local-only search does not query.
    for descriptor in fleet.descriptors:
        if retrieval_mode(descriptor) == "rag":
            print(
                f"note: {descriptor.name} exposes a tier-3 RAG endpoint "
                f"({descriptor.rag_endpoint}) not queried by local search",
                file=sys.stderr,
            )
    memories = [load_memory(d) for d in fleet.descriptors]
    # Extra sources from the fleet file (#247), searched and ranked alongside.
    sources = load_memory_sources(
        fleet.config.memory_sources, tuple(d.memory_path for d in fleet.descriptors)
    )
    for source in sources:
        # State coverage: a search that can only be wrong quietly names its denominator (#306).
        print(
            f"memory source {source.project}: {len(source.files)} file(s) indexed", file=sys.stderr
        )
        for warning in source.warnings:
            print(f"warning: {warning}", file=sys.stderr)
    hits = search_memory(memories + sources, " ".join(args.query))
    if args.json:
        return _emit_json([asdict(h) for h in hits])
    for hit in hits:
        location = f"{hit.file.project}/{hit.file.path.name}:{hit.line_number}"
        print(f"{location} [{hit.file.type}] {hit.file.name} — {hit.line}")
    if not hits:
        print("no matches")
    return 0


def _cmd_capabilities(args: argparse.Namespace) -> int:
    """Aggregate each project's CAPABILITIES.md — who exposes which skill/MCP."""
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    inventories = [load_capabilities(d) for d in selected]
    if args.json:
        return _emit_json([asdict(inv) for inv in inventories])
    if args.kind:
        index = aggregate_capabilities(inventories, args.kind)
        for name, projects in index.items():
            print(f"{name}: {', '.join(projects)}")
        if not index:
            print(f"no {args.kind} capabilities across the fleet")
        return 0
    for inventory in inventories:
        if not inventory.present:
            print(f"{inventory.project}: no CAPABILITIES.md")
            continue
        print(
            f"{inventory.project}: {len(inventory.skills)} skill(s), "
            f"{len(inventory.mcp_servers)} MCP server(s), {len(inventory.hooks)} hook(s)"
        )
    return 0


def _cmd_drift(args: argparse.Namespace) -> int:
    """Report scaffold drift; exit 1 when any project drifted."""
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    reports = [compute_drift(d) for d in selected]
    rc = 1 if any(r.status == "drift" for r in reports) else 0
    if args.json:
        _emit_json([asdict(r) for r in reports])
        return _unresolved_rc(fleet, rc)
    for report in reports:
        print(f"{report.project}: {report.summary}")
        for relpath in report.modified:
            print(f"  modified: {relpath}")
        for relpath in report.missing:
            print(f"  missing:  {relpath}")
    return _unresolved_rc(fleet, rc)


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnose contract-v1 conformance; exit 1 when any project fails."""
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    reports = [diagnose(d) for d in selected]
    rc = 1 if any(r.status == "fail" for r in reports) else 0
    if args.json:
        _emit_json([asdict(r) for r in reports])
        return _unresolved_rc(fleet, rc)
    for report in reports:
        print(f"{report.project}: {report.status}")
        for finding in report.findings:
            print(f"  [{finding.status}] {finding.check}: {finding.detail}")
    # The marker window closes when this reads zero: the `context:` key and the legacy marker file are
    # removed only after the fleet reports no repo still on them (marker contract).
    legacy = [d.name for d in selected if d.marker_key == "context"]
    if legacy:
        print(
            f"marker: {len(legacy)} of {len(selected)} repo(s) still on the legacy `context:` key: {', '.join(legacy)}"
        )
    else:
        print(f"marker: 0 of {len(selected)} repo(s) on the legacy `context:` key")
    return _unresolved_rc(fleet, rc)


def _emit_digest(args: argparse.Namespace, reports: list[AuditReport]) -> int:
    """Diff this audit against the last run, optionally push it, and report the delta."""
    digest = compute_digest(reports, load_prior())
    save_current(reports)
    payload = digest_payload(digest)
    # Only a *change* is worth pushing: a daily timer that posted "no change"
    # every morning would train the reader to ignore the channel.
    if args.webhook and digest.changed:
        ok = post_payload(args.webhook, payload)
        print(f"webhook: {'delivered' if ok else 'delivery failed'}", file=sys.stderr)
    rc = 1 if digest.new else 0
    if args.json:
        _emit_json(payload)
        return rc
    print(render_digest(digest))
    return rc


def _cmd_audit(args: argparse.Namespace) -> int:
    """Run the composed governance audit; exit 1 when anything needs attention."""
    if args.webhook and not args.digest:
        print("--webhook requires --digest (only a delta is worth pushing)", file=sys.stderr)
        return 2
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    cached = cache.load_results()
    reports = [audit_project(d, cached.get(d.name)) for d in selected]
    if args.digest:
        # SHORT-CIRCUIT BEFORE THE EMIT, not around it. `_emit_digest`
        # PERSISTS a new baseline and can post a webhook, and Python
        # evaluates an argument before the call it is passed to — so
        # wrapping it in `_unresolved_rc` ran the digest first. On an
        # unresolved fleet that marks every prior finding as resolved,
        # writes the empty baseline, and with --webhook PUBLISHES the
        # false resolution, returning 2 only afterwards. A transient
        # discovery failure would corrupt every later delta.
        if not fleet.descriptors:
            return NO_PROJECTS_RC
        return _emit_digest(args, reports)
    rc = 1 if any(r.needs_attention for r in reports) else 0
    if args.json:
        _emit_json([asdict(r) for r in reports])
        return _unresolved_rc(fleet, rc)
    if args.markdown:
        print(render_markdown(reports))
    else:
        for report in reports:
            print(f"{report.project}: {report.status}")
            for finding in report.findings:
                print(f"  [{finding.severity}] {finding.category}: {finding.message}")
    return _unresolved_rc(fleet, rc)


def _cmd_hardening(args: argparse.Namespace) -> int:
    """Show setup-readiness gaps with concrete next actions."""
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    reports = hardening_checklist(selected, cache.load_results())
    rc = 1 if any(report.needs_attention for report in reports) else 0
    if args.json:
        _emit_json([asdict(report) for report in reports])
        return _unresolved_rc(fleet, rc)
    print(render_hardening(reports))
    return _unresolved_rc(fleet, rc)


def _cmd_ci(args: argparse.Namespace) -> int:
    """Probe each project's CI conclusion + open-PR count via gh; cache them.

    Exits 1 when any project's CI has failed. Results are written to the
    checks cache so the ``status`` table shows last-known CI state offline.
    """
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    probes = map_ordered(probe_ci, selected)
    cache.save_results([r for _, results, _ in probes for r in results])
    rc = 1 if any(failed for _, _, failed in probes) else 0
    if args.json:
        _emit_json([payload for payload, _, _ in probes])
        return rc
    for payload, _results, _failed in probes:
        count = "?" if payload["count"] is None else str(payload["count"])
        print(f"{payload['project']}: CI {payload['ci']}, {count} open {payload['unit']}(s)")
    return rc


def _cmd_cloud_status(args: argparse.Namespace) -> int:
    """Probe deploy/runtime status per project (descriptor-driven); cache it.

    Exits 1 when any probed service is stopped or unhealthy. ``deploy: none``
    projects cost nothing (no subprocess, no network).
    """
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    checked_at = _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")
    statuses = map_ordered(collect_cloud, selected)
    cache.save_results([r for s in statuses for r in cloud_check_results(s, checked_at)])
    rc = 1 if any(s.health == "unhealthy" or s.state == "stopped" for s in statuses) else 0
    if args.json:
        _emit_json([asdict(s) for s in statuses])
        return rc
    for status in statuses:
        parts = [status.state]
        if status.revision:
            parts.append(status.revision)
        if status.health:
            parts.append(status.health)
        print(f"{status.project}: {status.target} — {' '.join(parts)}")
    return rc


def _cmd_events(args: argparse.Namespace) -> int:
    """Show guard/usage events across the fleet's observability logs."""
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    reports = [load_events(d) for d in selected]
    since = args.since or ""
    if args.json:
        payload = [
            {
                "project": r.project,
                "events": [asdict(e) for e in filter_since(r.events, since)],
                "warnings": list(r.warnings),
            }
            for r in reports
        ]
        return _emit_json(payload)
    empty = True
    for report in reports:
        for warning in report.warnings:
            print(f"warning: {report.project}: {warning}", file=sys.stderr)
        for event in filter_since(report.events, since):
            empty = False
            command = f" — {event.command}" if event.command else ""
            session = f" ({event.session})" if event.session else ""
            print(
                f"{event.project} {event.timestamp} [{event.hook}] {event.action}{session}{command}"
            )
    if empty:
        print("no events recorded")
    return 0


def _plugin_cell(row: UpgradeRow) -> str:
    """Render the plugin standing, e.g. ``plugin 0.9.16 → 0.9.20 behind`` (pure)."""
    if row.plugin_status == "behind":
        return f"plugin {row.plugin_version} → {row.plugin_latest} behind"
    return f"plugin {row.plugin_version} {row.plugin_status}"


def _cmd_upgrade_plan(args: argparse.Namespace) -> int:
    """Compare each project's scaffold version against upstream project-init.

    Exits 1 when any project is outdated. ``--apply`` dispatches each outdated
    child's own ``project-init-upgrade.yml`` workflow (never edits its tree).
    """
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    latest = latest_upstream_version(Path.cwd())
    rows = upgrade_plan(
        selected, latest, cache.load_results(), plugin_latest=latest_plugin_version(Path.cwd())
    )
    applied: dict[str, str] = {}
    if args.apply:
        by_name = {d.name: d for d in selected}
        applied = {
            row.project: trigger_upgrade(by_name[row.project])
            for row in rows
            if row.status == "outdated"
        }
    rc = 1 if any(r.status == "outdated" for r in rows) else 0
    if args.json:
        _emit_json([{**asdict(r), "applied": applied.get(r.project)} for r in rows])
        return rc
    for row in rows:
        line = (
            f"{row.project}: {row.status} "
            f"(scaffold {row.scaffold_version}, drift {row.drift}, PRs {row.open_prs}, "
            f"{_plugin_cell(row)})"
        )
        if row.reason:
            line += f" — {row.reason}"
        if row.project in applied:
            line += f" — upgrade {applied[row.project]}"
        print(line)
    return rc


def _cmd_register(args: argparse.Namespace) -> int:
    """Register a freshly-scaffolded project from `project-init --json` output.

    Consumes the project-init ``--json`` seam (#510): reads a scaffold-result
    document (a file path, or ``-`` for stdin) and adds the new project to the
    fleet file so the next command governs it — no manual edit, no second read.
    """
    raw = sys.stdin.read() if args.result == "-" else _read_text_or_none(args.result)
    if raw is None:
        print(f"cannot read scaffold result: {args.result}", file=sys.stderr)
        return 2
    result = parse_scaffold_result(raw)
    if result is None:
        print("not a project-init scaffold result (no 'target')", file=sys.stderr)
        return 1
    fleet_file = Path(args.fleet) if args.fleet else Path.cwd() / REGISTRY_FILENAME
    outcome = register_project(fleet_file, result.target)
    for warning in outcome.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    # A write failure surfaces as a warning with added=False; treat as an error.
    rc = 0 if outcome.added or not outcome.warnings else 1
    if args.json:
        _emit_json(
            {
                "target": str(outcome.project),
                "fleet_file": str(outcome.fleet_file),
                "added": outcome.added,
                "contract_version": result.contract_version,
                "files_created": result.files_created,
                "conflicts": list(result.conflicts),
            }
        )
        return rc
    verb = "registered" if outcome.added else "already registered"
    print(
        f"{verb} {outcome.project} in {outcome.fleet_file} "
        f"(contract v{result.contract_version}, {result.files_created} files)"
    )
    for conflict in result.conflicts:
        print(f"  scaffold conflict (left unwritten): {conflict}", file=sys.stderr)
    return rc


def _read_text_or_none(path: str) -> str | None:
    """Read a file's text, or ``None`` when it is unreadable."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        _log.debug("cannot read %s: %r", path, exc)
        return None


def _resolve_project(args: argparse.Namespace) -> ProjectDescriptor | None:
    """Resolve the required project argument, printing the error itself."""
    fleet = _discover(args)
    descriptor = fleet.get(args.project)
    if descriptor is None:
        print(f"unknown project: {args.project}", file=sys.stderr)
    return descriptor


def _cmd_deploy(args: argparse.Namespace) -> int:
    """Dispatch a project's deploy workflow for a cloud action (ADR-005).

    Dry-run by default: prints the plan and dispatches nothing. ``--apply``
    triggers the child's own ``workflow_dispatch`` pipeline, where production
    credentials live — the orchestrator holds none (project-init ADR-012). Exits 1 when an
    applied dispatch fails.

    Fire-and-forget by default (ADR-005): dispatch confirms the run was *queued*,
    not that it *succeeded*. ``--wait`` (with ``--apply``) closes that loop —
    poll the dispatched run to its conclusion and exit nonzero unless it actually
    succeeded.
    """
    descriptor = _resolve_project(args)
    if descriptor is None:
        return 2

    wait = args.wait and args.apply
    # Reject a wait we cannot honor BEFORE dispatching, not after. `gh` can follow
    # a GitHub run; `glab` pipelines are not polled, so on a GitLab project --wait
    # would fire the (irreversible) dispatch and only then report `unsupported`
    # and exit nonzero — and an automation retrying that nonzero would dispatch a
    # SECOND pipeline. Fail fast, before any mutation, so a retry is safe.
    if wait and provider_is_gitlab(descriptor):
        print(
            f"deploy: --wait is not supported for GitLab projects ({descriptor.name}) — "
            "glab pipelines are not polled. Re-run without --wait.",
            file=sys.stderr,
        )
        return 2

    # The watermark MUST be read before dispatch: it is the id of the newest run
    # that already existed, so wait_for_deploy can exclude it and follow only the
    # run this dispatch creates. Read it after dispatch and our own run could
    # already be listed, setting the floor above itself — we would then wait out
    # the whole timeout for a run we had, and report it unconfirmed.
    watermark = latest_run_id(descriptor, resolved_workflow(descriptor)) if wait else None

    result = trigger_deploy(descriptor, args.action, apply=args.apply)
    settlement = None
    if wait and result.status == DISPATCH_DISPATCHED:
        settlement = wait_for_deploy(descriptor, result.workflow, watermark)

    if args.json:
        # NOT `return _emit_json(...)`: _emit_json always returns 0, which would
        # hand every JSON consumer the exact success-that-wasn't this function
        # exists to prevent — `deploy --apply --json && notify "rolled back"`
        # announcing a rollback that never happened. Emit, then exit properly.
        payload = asdict(result)
        if settlement is not None:
            payload["settlement"] = asdict(settlement)
        _emit_json(payload)
        return _deploy_exit_code(result, settlement, apply=args.apply)
    workflow = f" via {result.workflow}" if result.workflow else ""
    detail = f" — {result.detail}" if result.detail else ""
    print(f"{result.project}: {result.action} {result.status}{workflow}{detail}")
    if settlement is not None:
        settle_detail = f" — {settlement.detail}" if settlement.detail else ""
        url = f" ({settlement.run_url})" if settlement.run_url else ""
        print(f"{settlement.project}: deploy {settlement.state}{settle_detail}{url}")
    return _deploy_exit_code(result, settlement, apply=args.apply)


def _deploy_exit_code(
    result: DeployDispatch, settlement: DeploySettlement | None, *, apply: bool
) -> int:
    """Exit nonzero whenever an explicit ``--apply`` did not reach success.

    A dry run reports and exits 0 — nothing was asked for. But under ``--apply``,
    `skipped` (no deploy target) and `no-workflow` are failures *to act*, not
    successes: `deploy api --action rollback --apply && notify "rolled back"`
    must not announce a rollback that never happened.

    Under ``--wait`` the bar rises from *dispatched* to *succeeded*: we watched
    the run, so anything short of a confirmed success — failed, timed-out,
    unconfirmed, unknown — is a nonzero exit. Waiting and then exiting 0 on a
    deploy we watched fail would be a worse lie than never having waited.
    """
    if result.status == DISPATCH_FAILED:
        return 1
    if apply and result.status != DISPATCH_DISPATCHED:
        return 1
    if settlement is not None and settlement.state != SETTLE_SUCCEEDED:
        return 1
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    """Launch a project's declared run_command, detached and logged."""
    descriptor = _resolve_project(args)
    if descriptor is None:
        return 2
    message = run_start(descriptor)
    print(message)
    # Match the outcome on the "(pid " anchor the success lines carry, so a
    # project named e.g. "restarted-service" whose failure line contains
    # "started" as a substring is not misread as success.
    return 0 if "(pid " in message else 1


def _cmd_stop(args: argparse.Namespace) -> int:
    """Terminate a project's supervised process."""
    descriptor = _resolve_project(args)
    if descriptor is None:
        return 2
    print(run_stop(descriptor))
    return 0


def _cmd_logs(args: argparse.Namespace) -> int:
    """Show the tail of a project's captured run output."""
    descriptor = _resolve_project(args)
    if descriptor is None:
        return 2
    for line in run_logs(descriptor, lines=args.lines):
        print(line)
    return 0


def _render_run(run: runs.AgentRun) -> str:
    """One line for an agent run: state, project, cost, task, and where to look."""
    where = run.pr_url or run.worktree or ""
    tail = f"  {where}" if where else ""
    spent = cost.format_usd(run.cost)
    return f"{run.state:<11} {run.project:<18} {spent:>7} {run.task[:44]}{tail}"


def _work_manage(args: argparse.Namespace) -> int | None:
    """Handle --list / --logs / --stop; ``None`` when none was asked for."""
    if args.list:
        found = work.list_runs(args.project or "")
        if not found:
            print("no agent runs")
            return 0
        for run in found:
            print(_render_run(run))
        print(f"\ntotal: {cost.format_total(cost.total(run.cost for run in found))}")
        return 0
    if args.logs:
        for line in work.logs(args.logs, lines=args.lines):
            print(line)
        return 0
    if args.stop:
        stopped = work.stop(args.stop)
        if stopped is None:
            print(f"unknown run: {args.stop}", file=sys.stderr)
            return 2
        print(_render_run(stopped))
        return 0
    if args.clear:
        outcome = work.clear(args.clear)
        if outcome == work.CLEARED:
            print(f"cleared {args.clear}")
            return 0
        reason = {
            work.CLEAR_UNKNOWN: "unknown run",
            work.CLEAR_ACTIVE: "it is still active — stop it first",
            work.CLEAR_FAILED: "its record could not be removed (check the state directory)",
        }[outcome]
        print(f"cannot clear {args.clear}: {reason}", file=sys.stderr)
        return 2
    return None


def _cmd_work(args: argparse.Namespace) -> int:
    """Launch a tracked agent run, or list / tail / stop / attach existing ones."""
    managed = _work_manage(args)
    if managed is not None:
        return managed

    if args.attach:
        if not args.project:
            print("usage: work <project> --attach", file=sys.stderr)
            return 2
        if work.needs_human_run(args.project) is None:
            print(f"no needs-human run for {args.project} to attach to", file=sys.stderr)
            return 2
        # Attach skips discovery, so load the fleet file here or its `models.attach`
        # override never becomes the active table (#324 review).
        _fleet_config(args)
        if work.attach(args.project) is None:
            print(
                f"could not open a session for {args.project} "
                "(is `claude` installed, and the run's worktree still present?)",
                file=sys.stderr,
            )
            return 2
        return 0

    # Guard the cap before either launch path (single or --where fan-out): a run
    # must never start under a budget the operator fat-fingered. ``nan``/``inf``
    # pass ``> 0`` (both ``<= 0`` comparisons are False), and an ``inf`` cap is no
    # cap — so reject non-finite values explicitly, not just non-positive ones.
    if not math.isfinite(args.budget) or args.budget <= 0:
        print(
            f"work: --budget must be a finite positive number, not {args.budget}", file=sys.stderr
        )
        return 2

    if args.where:
        return _cmd_work_fanout(args)

    # Launch on one project. Requires a project and a task.
    if not args.project or not args.task:
        print(
            'usage: work <project> "<task>"  (or --where FIELD=VALUE "<task>", '
            "--list / --logs ID / --stop ID)",
            file=sys.stderr,
        )
        return 2
    descriptor = _resolve_project(args)
    if descriptor is None:
        return 2
    run = work.launch(descriptor, args.task, args.budget)
    print(_render_run(run))
    # A run that failed to even start (no worktree, no wrapper) is a nonzero exit;
    # a launched run is success — its OUTCOME is observed later via --list.
    return 1 if run.state == runs.FAILED else 0


def _cmd_work_fanout(args: argparse.Namespace) -> int:
    """Launch one agent run per project matching ``--where``.

    **Dry-run by default.** A `--where` that matches forty projects would launch
    forty agents and spend forty agents' worth of money, and the most likely
    reason it matches forty projects is that the filter is wrong. So the default
    prints the target list and launches nothing; ``--apply`` commits to it. Same
    discipline as ``deploy`` (ADR-005), for the same reason.
    """
    # With --where there is no project positional, so the single positional the
    # operator typed IS the task: `work --where ci=fail "fix the CI"`. argparse
    # binds it to `project` (the first positional), so move it across — otherwise
    # the documented invocation is the one that does not work.
    task = args.task or args.project
    if not task:
        print('usage: work --where FIELD=VALUE "<task>"', file=sys.stderr)
        return 2

    selected = _select(args, fleet_snapshots(_discover(args)))
    if selected is None:
        return 2
    if not selected:
        print("no projects match — nothing to do")
        return 0

    if not args.apply:
        print(f"would launch an agent on {len(selected)} project(s):")
        for snapshot in selected:
            print(f"  {snapshot.descriptor.name}")
        print(f'\nre-run with --apply to launch: work --where ... "{task}" --apply')
        return 0

    failures = 0
    for snapshot in selected:
        run = work.launch(snapshot.descriptor, task, args.budget)
        print(_render_run(run))
        failures += run.state == runs.FAILED
    return 1 if failures else 0


def _cmd_run_agent(args: argparse.Namespace) -> int:
    """Hidden: the detached wrapper body. Not a verb an operator types.

    ``work.launch`` spawns ``projects-orchestrator _run-agent <run-id>`` as its
    detached process; this is what that process runs. It executes the agent and
    lands (or fails) the run, then exits — its exit code is incidental, because
    the run's terminal STATE, not this process's return, is the source of truth.
    """
    result = work.run_agent(args.run_id)
    return 0 if result.state == runs.PR_OPENED else 1


def _runs_by_project() -> dict[str, list[runs.AgentRun]]:
    """Group every recorded run by its project, for deriving campaign progress."""
    grouped: dict[str, list[runs.AgentRun]] = {}
    for run in runs.list_runs():
        grouped.setdefault(run.project, []).append(run)
    return grouped


def _campaign_fleet(args: argparse.Namespace, camp: campaign.Campaign) -> Fleet:
    """Discover the fleet, forcing plain-repo inclusion when the campaign needs it.

    A ``scaffold=none`` campaign targets repos with no project-init, which are
    invisible to discovery by default — so the campaign's own policy overrides the
    fleet config here, or its target list would be silently empty.
    """
    config = _fleet_config(args)
    if camp.policy.include_plain_repos:
        config = replace(config, include_plain_repos=True)
    fleet = discover(config)
    for warning in fleet.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return fleet


def _outcome_line(outcome: campaign.Outcome) -> str:
    """One rendered line for a campaign run outcome."""
    state = "timed-out" if outcome.timed_out else outcome.run.state
    where = outcome.run.pr_url or outcome.run.detail or ""
    tail = f"  {where}" if where else ""
    return f"{state:<11} {outcome.run.project:<18}{tail}"


def _campaign_json(report: campaign.CampaignReport) -> dict[str, object]:
    spend = report.spend
    return {
        "name": report.name,
        "canary": report.canary,
        "remaining": report.remaining,
        "outcomes": [
            {
                "run_id": o.run.id,
                "project": o.run.project,
                "state": o.run.state,
                "pr_url": o.run.pr_url,
                "timed_out": o.timed_out,
                "detail": o.run.detail,
                # null, not 0.0 — an unmetered run's cost is unknown, not free.
                "cost": asdict(o.run.cost) if o.run.cost else None,
            }
            for o in report.outcomes
        ],
        "spend": asdict(spend),
    }


def _render_campaign_report(report: campaign.CampaignReport) -> None:
    """Print a campaign's outcomes, its spend, then the canary's you-still-have-N nudge."""
    for outcome in report.outcomes:
        print(_outcome_line(outcome))

    spend = report.spend
    print(f"\nspend: {cost.format_total(spend)}")
    if report.canary and report.remaining:
        line = (
            f"canary complete — {report.remaining} project(s) still outstanding. "
            "Review the PR above, then re-run with --apply to fan out to the rest."
        )
        # The canary's measured cost is the best per-project estimate available, so
        # quote what the fan-out would add BEFORE the operator authorises it. Only
        # when something was actually metered: projecting from zero metered runs
        # would invent a number, and inventing a cheap one is how a fan-out gets
        # approved on evidence that never existed.
        if spend.metered:
            projected = spend.usd / spend.metered * report.remaining
            line += f"\n--apply would add roughly ${projected:.2f} at the canary's rate."
        print(line)


def _cmd_campaign(args: argparse.Namespace) -> int:
    """Run a declarative campaign: canary one project by default, --apply for all.

    Progress is derived, not tracked: what is outstanding is recomputed each run
    from the selector and the runs store, so a campaign whose selector matches
    nothing is done (exit 0), and re-running picks up only what still remains.
    """
    try:
        camp = campaign.resolve(args.file)
    except campaign.CampaignError as exc:
        print(f"campaign: {exc}", file=sys.stderr)
        return 2

    snapshots = fleet_snapshots(_campaign_fleet(args, camp))
    targets = campaign.outstanding(camp, snapshots, _runs_by_project())
    if not targets:
        print(f"campaign '{camp.name}' is done — nothing outstanding")
        return 0
    if args.dry_run:
        print(f"campaign '{camp.name}': {len(targets)} project(s) outstanding")
        for snapshot in targets:
            print(f"  {snapshot.descriptor.name}")
        print("\nre-run without --dry-run to canary one, or with --apply to fan out.")
        return 0

    batch = targets if args.apply else targets[:1]
    outcomes = campaign.execute(camp, [s.descriptor for s in batch], campaign.default_seams())
    report = campaign.summarize(
        camp, outcomes, remaining=len(targets) - len(batch), canary=not args.apply
    )
    # The exit code carries the outcome to automation and must survive the render
    # format: `_emit_json` always returns 0, so a failed run reported as JSON would
    # otherwise look like success while the text path correctly fails.
    exit_code = 0 if report.ok else 1
    if args.json or camp.policy.output == "json":
        _emit_json(_campaign_json(report))
        return exit_code
    _render_campaign_report(report)
    return exit_code


def _heal_targets(
    descriptors: list[ProjectDescriptor], *, cached: bool, jobs: int | None
) -> list[tuple[ProjectDescriptor, dict[str, CheckResult]]]:
    """Pair each project with its lint/test results for a heal pass.

    Fresh by default: runs the two healable gates now — on a bounded pool, like
    ``checks`` — and saves them to the cache (so a later ``status``/``checks``
    agrees) so heal acts on current state rather than a stale cache. ``--cached``
    skips that run and reuses the last recorded results: the fast path right after
    a ``checks`` run has already populated the cache.
    """
    if cached:
        store = cache.load_results()
        return [(descriptor, store.get(descriptor.name, {})) for descriptor in descriptors]
    # Stamped with the clean-tree HEAD, exactly as `checks` stamps it: the notify
    # issue sink files and closes only on results taken at a commit, never on
    # uncommitted work in progress (#164 review).
    fresh = [
        result
        for project_results in map_ordered(
            lambda d: [result for result, _ in _project_checks(d, HEALABLE_TASKS, None, False)],
            descriptors,
            jobs=jobs,
        )
        for result in project_results
    ]
    cache.save_results(fresh)
    by_project: dict[str, dict[str, CheckResult]] = {}
    for result in fresh:
        by_project.setdefault(result.project, {})[result.task] = result
    return [(descriptor, by_project.get(descriptor.name, {})) for descriptor in descriptors]


def _fleet_heal_json(report: FleetHealReport) -> dict[str, object]:
    """Machine-readable form of a fleet heal pass (for ``--json``)."""
    return {
        "attempted": [asdict(result) for result in report.results],
        "deferred": list(report.deferred),
        "limit": report.limit,
        "fixed": len(report.fixed),
        "eventful": report.eventful,
        "spend": asdict(report.spend),
    }


def _cmd_heal(args: argparse.Namespace) -> int:
    """Heal a failing lint/test gate: one project, or the whole fleet (``--all``).

    Runs the two healable gates now (unless ``--cached``), then spawns a scoped
    agent per failing project and, on a re-verified fix, opens a draft PR — never
    touching a default branch (ADR-006). ``--all`` is the scheduled trigger's
    entry point and ``--limit`` caps how many projects one unattended pass spends
    on.

    Exit codes suit a scheduler: **1** when the pass was eventful (it attempted a
    heal or deferred a project), **0** when the fleet had nothing to heal, **2**
    on a usage error. The systemd unit maps 1 to success, so an eventful nightly
    run is not flagged as failed.

    ``--webhook`` hands the finished report to :func:`notify.heal_webhook_sink`,
    so an unattended pass tells the operator what it fixed (PR URL) or why it
    could not, while the PR itself stays a draft (#165). A clean pass posts
    nothing. ``--issues`` hands it to :func:`heal_issues.heal_issue_sink`, which
    files one issue per failing gate of each notify-mode project and closes it
    once the gate passes (#164). Each delivery status goes to stderr and the exit
    code is unchanged: a sink that is down must not turn a successful heal into a
    failed unit.
    """
    if bool(args.project) == args.all:
        print(
            "heal: name one project, or pass --all for the whole fleet (not both)",
            file=sys.stderr,
        )
        return 2
    if args.all and args.limit < 1:
        print("heal: --limit must be at least 1", file=sys.stderr)
        return 2

    fleet = _discover(args)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        descriptors, limit = [descriptor], 1
    else:
        descriptors, limit = list(fleet.descriptors), args.limit

    targets = _heal_targets(descriptors, cached=args.cached, jobs=args.jobs)
    report = heal_fleet(targets, limit=limit, mode=args.mode)
    deliveries = {
        "webhook": heal_webhook_sink(args.webhook)(report) if args.webhook else None,
        "issues": heal_issue_sink(targets, args.mode)(report) if args.issues else None,
    }
    if args.json:
        _emit_json(
            {
                **_fleet_heal_json(report),
                **{
                    sink: None if ok is None else ("delivered" if ok else "failed")
                    for sink, ok in deliveries.items()
                },
            }
        )
    else:
        print(render_fleet_heal_report(report))
    for sink, ok in deliveries.items():
        if ok is not None:
            print(f"{sink}: {'delivered' if ok else 'delivery failed'}", file=sys.stderr)
    return 1 if report.eventful else 0


def _cmd_orphans(args: argparse.Namespace) -> int:
    """Report live GCP resources that no repo in the fleet accounts for.

    Read-only: it lists the cloud asset inventory and diffs it against the fleet,
    and mutates nothing. A scan that could not run reports UNKNOWN and exits
    nonzero — it never prints "no orphans", which would falsely clear an estate
    that was never looked at (ADR-003).
    """
    fleet = _discover(args)
    if not args.scope:
        # No scope means gcloud would inventory only the configured project — a
        # partial scan a multi-project fleet must not mistake for a complete one.
        # Refuse rather than declare a clean estate we never fully looked at.
        print(
            "orphans: a --scope is required (projects/<id>, folders/<id>, or "
            "organizations/<id>) — an unscoped scan covers only the configured "
            "project and cannot declare the fleet's estate clean.",
            file=sys.stderr,
        )
        return 2
    report = orphans.find_orphans(fleet, gcp.search_resources(args.scope))
    if report.is_unknown:
        print(
            "orphans: unknown — could not read the GCP inventory "
            "(is `gcloud` installed and authenticated?). NOT reporting zero orphans.",
            file=sys.stderr,
        )
        return 1
    if args.json:
        return _emit_json([asdict(o) for o in report.orphans])
    if not report.orphans:
        print("no orphans — every live GCP resource is accounted for by the fleet")
        return 0
    print(f"{len(report.orphans)} orphan(s) — live in GCP, governed by no repo:")
    for orphan in report.orphans:
        where = f" [{orphan.project}]" if orphan.project else ""
        print(f"  {orphan.display_name or orphan.name}  ({orphan.asset_type}){where}")
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    """Dump the full joined fleet view (text, JSON, or standalone HTML)."""
    fleet = _discover(args)
    snapshots = fleet_snapshots(fleet)
    if args.json:
        return _emit_json([asdict(s) for s in snapshots])
    # -o implies --html: writing the text table to a .html file the operator
    # named would silently produce a non-page, so treat an output path as a
    # request for the HTML document.
    if args.html or args.output:
        generated_at = _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")
        document = render_html(
            fleet_rows(snapshots), generated_at, host_health(fleet.config.host_health_command)
        )
        if args.output:
            try:
                Path(args.output).write_text(document, encoding="utf-8")
            except OSError as exc:
                print(f"cannot write {args.output}: {exc}", file=sys.stderr)
                return 1
            print(f"wrote {args.output}")
        else:
            print(document, end="")
        return 0
    print(render_table(fleet_rows(snapshots)))
    print(host_health(fleet.config.host_health_command))
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    """Show per-task check-history trends and pass/fail transitions for a project."""
    fleet = _discover(args)
    descriptor = fleet.get(args.project)
    if descriptor is None:
        print(f"unknown project: {args.project}", file=sys.stderr)
        return 2
    by_task = project_history(load_history(), descriptor.name)
    if args.json:
        return _emit_json(
            {
                task: {
                    "trend": sparkline(entries, args.width),
                    "transitions": [asdict(e) for e in transitions(entries)],
                }
                for task, entries in by_task.items()
            }
        )
    if not by_task:
        print(f"{descriptor.name}: no check history yet")
        return 0
    for task in sorted(by_task):
        entries = by_task[task]
        print(f"{task}: {sparkline(entries, args.width)}  ({len(entries)} run(s))")
        last = transitions(entries)[-1] if transitions(entries) else None
        if last is not None:
            print(f"  last change: {last.status} at {last.checked_at}")
    return 0


def _cmd_notify(args: argparse.Namespace) -> int:
    """Compute threshold alerts and optionally push them to a webhook."""
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    snapshots = [s for s in fleet_snapshots(fleet) if s.descriptor in selected]
    alerts = fleet_alerts(snapshots)
    if args.json:
        _emit_json({**alerts_payload(alerts), "watch": asdict(watchdog.read_state())})
    else:
        _print_watch_state()
        print(render_alerts(alerts))
    if args.webhook and alerts:
        ok = post_webhook(args.webhook, alerts)
        print(f"webhook: {'delivered' if ok else 'delivery failed'}", file=sys.stderr)
    return 1 if alerts else 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """One scheduled watch pass: refresh gates, record history, push alerts.

    Chains what ``checks``, ``ci``, ``cloud-status`` and ``notify`` do
    separately, so a timer needs a single invocation: run the declared gates
    fleet-wide, probe each project's CI, cloud state, and supervised-process
    liveness, merge everything into the cache, append it to history, then
    compute threshold alerts from the refreshed fleet view and (optionally)
    POST them to a webhook.

    The remote probes always run — ``--changed-only`` economizes on the local
    gates only, because a cached pass at an unchanged HEAD says nothing about
    a forge or a deployment, whose state moves without any local edit. An
    hourly watch that read CI/cloud from the last manual run would sleep
    through exactly the reds it exists to notice.

    Exits **1** when the pass was eventful (any alert — something for the
    operator to look at), **0** when the fleet is within thresholds, and **2**
    when no fleet was found: a watch timer pointed at an empty root is a
    misconfiguration that must not masquerade as a permanently quiet fleet.
    """
    fleet = _discover(args)
    if not fleet.descriptors:
        print(
            "watch: no projects discovered — check --root/--fleet (or PO_FLEET_ROOT)",
            file=sys.stderr,
        )
        return 2
    descriptors = list(fleet.descriptors)
    tasks = tuple(args.task) if args.task else DEFAULT_TASKS
    cached = cache.load_results() if args.changed_only else {}
    per_project = map_ordered(
        lambda d: _project_checks(d, tasks, cached.get(d.name), args.changed_only),
        descriptors,
        jobs=args.jobs,
    )
    pairs = [pair for project_pairs in per_project for pair in project_pairs]
    fresh = [result for result, reused in pairs if not reused]
    probes = map_ordered(probe_ci, descriptors, jobs=args.jobs)
    checked_at = _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")
    statuses = map_ordered(collect_cloud, descriptors, jobs=args.jobs)
    remote = [r for _, results, _ in probes for r in results] + [
        r for s in statuses for r in cloud_check_results(s, checked_at)
    ]
    # Supervised-process liveness joins the pass as one more probe. Where it
    # is ABSENT (nothing supervised), the stale cache entry is retired rather
    # than left as a last-known value: a process check must mean "supervised,
    # and this is its health" — never "was supervised once". A project that
    # declares its own `process` tooling gate owns that task key outright:
    # the probe neither overwrites nor retires a user-declared gate's result.
    for descriptor in descriptors:
        if "process" in descriptor.tooling:
            continue
        result = liveness_check(descriptor, checked_at)
        if result is None:
            cache.drop_result(descriptor.name, "process")
        else:
            remote.append(result)
    cache.save_results(fresh + remote)
    history_record(fresh + remote)
    # Record the PASS, not just its results (#186). History only carries results
    # whose status is recordable, so a pass over a fleet that produced none —
    # everything skipped, every gate undeclared — left no trace and was
    # indistinguishable from a pass that never happened.
    watchdog.record_pass(args.interval)
    alerts = fleet_alerts(fleet_snapshots(fleet))
    delivered = post_webhook(args.webhook, alerts) if args.webhook and alerts else None
    if args.json:
        _emit_json(
            {
                "checks": [{**asdict(r), "cached": reused} for r, reused in pairs]
                + [{**asdict(r), "cached": False} for r in remote],
                **alerts_payload(alerts),
                "webhook": None if delivered is None else ("delivered" if delivered else "failed"),
            }
        )
        return 1 if alerts else 0
    reused_count = len(pairs) - len(fresh)
    cached_note = f" ({reused_count} cached)" if reused_count else ""
    print(
        f"watch: {len(fleet.descriptors)} project(s), "
        f"{len(fresh)} gate(s) refreshed{cached_note}, "
        f"{len(remote)} remote probe(s), {len(alerts)} alert(s)"
    )
    print(render_alerts(alerts))
    if delivered is not None:
        print(f"webhook: {'delivered' if delivered else 'delivery failed'}", file=sys.stderr)
    return 1 if alerts else 0


def _cmd_serve(args: argparse.Namespace) -> int:
    """Serve the live fleet dashboard over HTTP until interrupted."""
    if args.enable_actions and not is_loopback(args.host):
        print(
            f"serve: --enable-actions needs a loopback --host (127.0.0.1 or ::1); "
            f"refusing to expose mutating actions on {args.host}",
            file=sys.stderr,
        )
        return 2
    serve(
        _fleet_config(args),
        host=args.host,
        port=args.port,
        enable_actions=args.enable_actions,
    )
    return 0


def _cmd_controller(args: argparse.Namespace) -> int:
    """Run the deterministic command REPL."""
    ctx = ControllerContext(config=_fleet_config(args))
    print(f"fleet: {len(ctx.fleet.descriptors)} project(s) — type 'help' for commands")
    while True:
        try:
            line = input("orchestrator> ")
        except (EOFError, KeyboardInterrupt):
            # expected: end of input or Ctrl-C is how an operator leaves the REPL
            print()
            return 0
        intent = parse_command(line)
        if intent.verb == "quit":
            return 0
        for output in dispatch(intent, ctx):
            print(output)


def _cmd_tui(args: argparse.Namespace) -> int:
    """Launch the Textual TUI (requires the ``tui`` extra)."""
    try:
        from projects_orchestrator.tui import OrchestratorApp
    except ModuleNotFoundError:
        # expected: the tui extra is not installed, and the message below says so
        print(
            "the TUI needs the optional dependency: uv sync --extra tui "
            "(or: pip install 'projects-orchestrator[tui]')",
            file=sys.stderr,
        )
        return 2
    OrchestratorApp(config=_fleet_config(args)).run()
    return 0


_VERBOSE_HELP = (
    f"log why each degraded cell degraded, and show a traceback on an internal error "
    f"(also: {VERBOSE_ENV}=1)"
)


def _add_common(parser: argparse.ArgumentParser, json_flag: bool = True) -> None:
    """Attach the shared --fleet/--root (and usually --json) options."""
    parser.add_argument("--fleet", help="path to a fleet.yaml describing the fleet")
    parser.add_argument("--root", help="directory scanned one level deep for projects")
    # SUPPRESS, so `cmd --verbose` and `--verbose cmd` both work and neither
    # position's absence overwrites the other's presence.
    parser.add_argument(
        "--verbose", action="store_true", default=argparse.SUPPRESS, help=_VERBOSE_HELP
    )
    if json_flag:
        parser.add_argument("--json", action="store_true", help="emit JSON instead of text")


def _add_where(parser: argparse.ArgumentParser) -> None:
    """Attach the repeatable ``--where`` selector (AND across repeats)."""
    parser.add_argument(
        "--where",
        action="append",
        metavar="FIELD=VALUE",
        help=(
            "filter the fleet on what it already knows (repeatable, AND). "
            f"e.g. ci=fail, scaffold=none, drift>0. Fields: {', '.join(selector.FIELDS)}"
        ),
    )


def _select(
    args: argparse.Namespace, snapshots: list[ProjectSnapshot]
) -> list[ProjectSnapshot] | None:
    """Apply ``--where``; print the error and return ``None`` when it is nonsense.

    A mistyped filter must never quietly select nothing (which looks like a
    healthy fleet) or everything (which is worse). The caller exits 2.
    """
    try:
        return selector.select(snapshots, getattr(args, "where", None) or [])
    except selector.SelectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _add_heal_arguments(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire `heal`'s single-project / fleet-wide flags onto its subparser."""
    heal_sp = sub.choices["heal"]
    heal_sp.add_argument("project", nargs="?", help="the project to heal (omit with --all)")
    heal_sp.add_argument(
        "--all",
        action="store_true",
        help="heal every project in the fleet with a failing lint/test gate",
    )
    heal_sp.add_argument(
        "--limit",
        type=int,
        default=5,
        help="with --all, max projects to attempt in one pass (caps unattended spend; default 5)",
    )
    heal_sp.add_argument(
        "--cached",
        action="store_true",
        help="heal from the last recorded checks instead of running the gates fresh",
    )
    heal_sp.add_argument(
        "--jobs",
        type=int,
        help="parallel projects when running fresh checks (default: min(8, cpu))",
    )
    heal_sp.add_argument(
        "--mode",
        choices=HEAL_MODES,
        default=MODE_FIX,
        help="run-wide heal mode: fix spawns the scoped agent and opens a draft PR "
        "(default); notify only reports what failed and what to do next. A project's "
        "declared heal.mode overrides this (ADR-008)",
    )
    heal_sp.add_argument(
        "--webhook",
        help="POST what the pass did (PR URLs, or why a heal failed) as JSON to this URL "
        "(Slack-compatible); a pass with nothing failing posts nothing",
    )
    heal_sp.add_argument(
        "--issues",
        action="store_true",
        help="for each notify-mode project, file one GitHub issue per failing gate on its "
        "repository (deduplicated against its open issues), and close the issue once "
        "the gate passes (ADR-008)",
    )


def _add_work_arguments(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire `work`'s own flags (and the hidden runner's) onto the subparsers."""
    # --where filters on what the fleet table already knows; it probes nothing.
    for name in ("status", "work"):
        _add_where(sub.choices[name])
    work_sp = sub.choices["work"]
    work_sp.add_argument("project", nargs="?", help="project to launch a run on (or filter --list)")
    work_sp.add_argument("task", nargs="?", help="what the agent should do")
    work_sp.add_argument(
        "--apply",
        action="store_true",
        help="with --where: actually launch the fan-out (default: print the targets only)",
    )
    work_sp.add_argument("--list", action="store_true", help="list agent runs instead of launching")
    work_sp.add_argument("--logs", metavar="RUN_ID", help="tail one run's captured output")
    work_sp.add_argument(
        "--stop", metavar="RUN_ID", help="kill a running agent and mark it abandoned"
    )
    work_sp.add_argument(
        "--clear",
        metavar="RUN_ID",
        help="forget a settled run (e.g. after its PR merged) so it leaves the Work column",
    )
    work_sp.add_argument(
        "--attach",
        action="store_true",
        help="open an interactive session in a project's needs-human run worktree",
    )
    work_sp.add_argument(
        "-n", "--lines", type=int, default=work.DEFAULT_LOG_LINES, help="trailing --logs lines"
    )
    work_sp.add_argument(
        "--budget",
        type=float,
        default=work.DEFAULT_BUDGET_USD,
        metavar="USD",
        help=f"per-run spend cap in USD (default {work.DEFAULT_BUDGET_USD:.2f})",
    )
    sub.choices[work.RUNNER_SUBCOMMAND].add_argument("run_id")


def _top_level_parser() -> argparse.ArgumentParser:
    """The root parser and its global flags, before any subcommand is attached."""
    parser = argparse.ArgumentParser(
        prog="projects-orchestrator",
        description="Cross-project orchestration layer for agentic development.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--verbose", action="store_true", help=_VERBOSE_HELP)
    return parser


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser with all subcommands."""
    parser = _top_level_parser()
    sub = parser.add_subparsers(dest="command")

    specs: list[tuple[str, str, object, bool]] = [
        ("projects", "list discovered projects", _cmd_projects, True),
        ("status", "fleet git health (table) or one project", _cmd_status, True),
        ("checks", "run each project's declared gates", _cmd_checks, True),
        (
            "test-fleet",
            "run every repo's `just test` and report passed/failed/exit per repo",
            _cmd_test_fleet,
            True,
        ),
        ("memory", "search all project memories", _cmd_memory, True),
        (
            "capabilities",
            "aggregate CAPABILITIES.md — who exposes which skill/MCP/hook",
            _cmd_capabilities,
            True,
        ),
        ("drift", "scaffold drift vs the recorded manifest", _cmd_drift, True),
        ("doctor", "diagnose contract-v1 conformance", _cmd_doctor, True),
        (
            "audit",
            "composed governance report (conformance + drift + memory + freshness)",
            _cmd_audit,
            True,
        ),
        (
            "hardening",
            "setup-readiness checklist with concrete next actions",
            _cmd_hardening,
            True,
        ),
        ("ci", "latest CI conclusion + open-PR count per project (via gh)", _cmd_ci, True),
        (
            "cloud-status",
            "deploy/runtime status per project (descriptor deploy block)",
            _cmd_cloud_status,
            True,
        ),
        ("events", "guard/usage events from the fleet's observability logs", _cmd_events, True),
        ("history", "per-task check-history trend and pass/fail transitions", _cmd_history, True),
        (
            "notify",
            "threshold alerts (CI red, drift, hooks, cloud) — optionally to a webhook",
            _cmd_notify,
            True,
        ),
        (
            "watch",
            "one scheduled pass: refresh gates, record history, push threshold alerts",
            _cmd_watch,
            True,
        ),
        (
            "deploy",
            "dispatch a project's deploy workflow (deploy/rollback/restart; --apply)",
            _cmd_deploy,
            True,
        ),
        ("start", "launch a project's run_command (detached, logged)", _cmd_start, False),
        ("stop", "terminate a project's supervised process", _cmd_stop, False),
        ("logs", "tail a project's captured run output", _cmd_logs, False),
        (
            "work",
            "launch a tracked agent run on a project (or --list / --logs / --stop)",
            _cmd_work,
            False,
        ),
        (work.RUNNER_SUBCOMMAND, argparse.SUPPRESS, _cmd_run_agent, False),
        (
            "campaign",
            "run a declarative fleet campaign (canary by default; --apply to fan out)",
            _cmd_campaign,
            True,
        ),
        (
            "heal",
            "fix a failing lint/test gate: one project, or the fleet (--all); opens a PR",
            _cmd_heal,
            True,
        ),
        (
            "upgrade-plan",
            "scaffold version vs upstream project-init (--apply triggers upgrades)",
            _cmd_upgrade_plan,
            True,
        ),
        (
            "register",
            f"register a scaffolded project from `{SCAFFOLD_JSON_COMMAND}` output",
            _cmd_register,
            True,
        ),
        (
            "orphans",
            "live GCP resources no repo in the fleet accounts for (read-only)",
            _cmd_orphans,
            True,
        ),
        ("snapshot", "full joined fleet view", _cmd_snapshot, True),
        ("serve", "serve the live fleet dashboard over HTTP", _cmd_serve, False),
        ("controller", "interactive deterministic command REPL", _cmd_controller, False),
        ("tui", "terminal UI (requires the tui extra)", _cmd_tui, False),
    ]
    for name, help_text, handler, json_flag in specs:
        sp = sub.add_parser(name, help=help_text)
        _add_common(sp, json_flag=json_flag)
        sp.set_defaults(handler=handler)

    sub.choices["status"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["checks"].add_argument("project", nargs="?", help="limit to one project")
    _add_test_fleet_arguments(sub)
    sub.choices["capabilities"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["capabilities"].add_argument(
        "--kind",
        choices=(SKILL, MCP, HOOK),
        help="invert the fleet: list each capability of this kind and who exposes it",
    )
    sub.choices["drift"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["doctor"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["audit"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["hardening"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["audit"].add_argument(
        "--markdown", action="store_true", help="render the report as Markdown"
    )
    sub.choices["audit"].add_argument(
        "--digest",
        action="store_true",
        help="show only what changed since the last audit run (exit 1 on new issues)",
    )
    sub.choices["audit"].add_argument(
        "--webhook",
        help="POST the digest delta as JSON to this URL (Slack-compatible); requires --digest",
    )
    sub.choices["ci"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["cloud-status"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["events"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["events"].add_argument("--since", help="only events at/after this ISO-8601 instant")
    sub.choices["history"].add_argument("project", help="project to show history for")
    sub.choices["history"].add_argument(
        "-n", "--width", type=int, default=HISTORY_TREND_WIDTH, help="trend width (default 10)"
    )
    sub.choices["notify"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["notify"].add_argument(
        "--webhook", help="POST alerts as JSON to this URL (Slack-compatible)"
    )
    _add_watch_arguments(sub)
    for name in ("start", "stop", "logs", "deploy"):
        sub.choices[name].add_argument("project", help="project to act on")
    sub.choices["deploy"].add_argument(
        "--action",
        choices=DEPLOY_ACTIONS,
        default="deploy",
        help="cloud action to dispatch (default: deploy)",
    )
    sub.choices["deploy"].add_argument(
        "--apply",
        action="store_true",
        help="actually dispatch the workflow (default: dry-run plan only)",
    )
    sub.choices["deploy"].add_argument(
        "--wait",
        action="store_true",
        help="with --apply: poll the dispatched run to its conclusion "
        "(succeeded/failed/timed-out); exit nonzero unless it succeeded",
    )
    sub.choices["logs"].add_argument(
        "-n", "--lines", type=int, default=40, help="trailing lines to show (default 40)"
    )
    sub.choices["orphans"].add_argument(
        "--scope",
        help="GCP scope to inventory: projects/<id>, folders/<id>, or organizations/<id>",
    )
    _add_work_arguments(sub)
    campaign_sp = sub.choices["campaign"]
    campaign_sp.add_argument(
        "file",
        help="a built-in campaign name (e.g. project-init) or a path to a campaign YAML file",
    )
    campaign_sp.add_argument(
        "--apply",
        action="store_true",
        help="fan out to every outstanding project (default: canary one and stop)",
    )
    campaign_sp.add_argument(
        "--dry-run",
        action="store_true",
        help="list outstanding projects and launch nothing",
    )
    _add_heal_arguments(sub)
    sub.choices["register"].add_argument(
        "result", help=f"path to `{SCAFFOLD_JSON_COMMAND}` output, or '-' for stdin"
    )
    sub.choices["upgrade-plan"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["upgrade-plan"].add_argument(
        "--apply", action="store_true", help="dispatch the upgrade workflow for outdated projects"
    )
    sub.choices["snapshot"].add_argument(
        "--html", action="store_true", help="render a self-contained HTML dashboard"
    )
    sub.choices["snapshot"].add_argument(
        "-o", "--output", help="write the HTML to this file instead of stdout"
    )
    sub.choices["checks"].add_argument(
        "--task", action="append", help="gate to run (repeatable; default: lint, test)"
    )
    sub.choices["checks"].add_argument(
        "--jobs", type=int, help="parallel projects (default: min(8, cpu count))"
    )
    sub.choices["checks"].add_argument(
        "--changed-only",
        action="store_true",
        help="skip gates whose last cached pass is at the current clean HEAD",
    )
    sub.choices["memory"].add_argument("query", nargs="+", help="text to search for")
    _add_serve_arguments(sub)
    return parser


def _add_test_fleet_arguments(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Attach `test-fleet`'s project filter, per-repo timeout and parallelism."""
    test_fleet_sp = sub.choices["test-fleet"]
    test_fleet_sp.add_argument("project", nargs="?", help="limit to one project")
    test_fleet_sp.add_argument(
        "--timeout",
        type=float,
        default=FLEET_TEST_TIMEOUT,
        help=f"seconds before a repo's run is killed (default {FLEET_TEST_TIMEOUT:.0f})",
    )
    test_fleet_sp.add_argument(
        "--jobs", type=int, default=1, help="repos run at once (default 1: suites fan out already)"
    )


def _add_watch_arguments(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire `watch`'s gate-selection and webhook flags onto its subparser."""
    watch_sp = sub.choices["watch"]
    watch_sp.add_argument("--webhook", help="POST alerts as JSON to this URL (Slack-compatible)")
    watch_sp.add_argument(
        "--interval",
        type=int,
        default=0,
        metavar="SECONDS",
        help=(
            "the schedule this pass runs on; lets `status` and `notify` tell a stale "
            "watch from a fresh one. Omitted leaves staleness unjudgeable rather than guessed"
        ),
    )
    watch_sp.add_argument(
        "--task", action="append", help="gate to run (repeatable; default: lint, test)"
    )
    watch_sp.add_argument("--jobs", type=int, help="parallel projects (default: min(8, cpu count))")
    watch_sp.add_argument(
        "--changed-only",
        action="store_true",
        help="skip gates whose last cached pass is at the current clean HEAD",
    )


def _add_serve_arguments(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire `serve`'s bind and opt-in mutating-actions flags onto its subparser."""
    serve_sp = sub.choices["serve"]
    serve_sp.add_argument(
        "--host", default=DEFAULT_HOST, help=f"bind host (default {DEFAULT_HOST})"
    )
    serve_sp.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"bind port (default {DEFAULT_PORT})"
    )
    serve_sp.add_argument(
        "--enable-actions",
        action="store_true",
        help="allow re-check/heal from the page (loopback bind only; CSRF-token gated)",
    )


def _verbose(args: argparse.Namespace) -> bool:
    """Whether the diagnostic trail is on: the flag, or the env toggle."""
    if getattr(args, "verbose", False):
        return True
    return os.environ.get(VERBOSE_ENV, "").strip().lower() in _TRUTHY


_PACKAGE_LOGGER = "projects_orchestrator"


def _package_loggers() -> list[logging.Logger]:
    """The package logger and every module logger under it that exists so far."""
    prefix = f"{_PACKAGE_LOGGER}."
    children = [
        logger
        for name, logger in list(logging.Logger.manager.loggerDict.items())
        if name.startswith(prefix) and isinstance(logger, logging.Logger)
    ]
    return [logging.getLogger(_PACKAGE_LOGGER), *children]


@contextlib.contextmanager
def _diagnostic_trail(verbose: bool) -> Iterator[None]:
    """Own the package's debug records for one command, then hand them back.

    Degraded paths log at DEBUG. While a command runs, every logger in the
    package, the module loggers included, sets aside the handlers a host
    attached to it and defers to the package logger, which stops propagating.
    A host that configured logging at DEBUG, on the root, the package or any one
    module, then neither sees the trail on a quiet run nor prints each line twice
    on a verbose one (Codex on #269). Quiet: no handler at all, so a default run
    prints exactly what it did before the seam existed. Verbose: one stderr
    handler. Everything is restored on the way out, so a repeated ``main()``
    never stacks a second handler and an embedding caller gets its loggers back.
    """
    loggers = _package_loggers()
    saved = [(lg, list(lg.handlers), lg.level, lg.propagate) for lg in loggers]
    for lg in loggers:
        for existing in list(lg.handlers):
            lg.removeHandler(existing)
        lg.setLevel(logging.NOTSET)
        lg.propagate = True
    package = loggers[0]
    package.propagate = False
    handler: logging.Handler | None = None
    if verbose:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("debug: %(name)s: %(message)s"))
        package.addHandler(handler)
        package.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        if handler is not None:
            package.removeHandler(handler)
        for lg, handlers, level, propagate in saved:
            for existing in handlers:
                lg.addHandler(existing)
            lg.setLevel(level)
            lg.propagate = propagate


def _internal_error(command: str, exc: Exception, verbose: bool) -> int:
    """Report an exception that escaped a command: one line, traceback on request."""
    if verbose:
        traceback.print_exception(exc, file=sys.stderr)
    hint = "" if verbose else "; rerun with --verbose for the traceback"
    detail = f"{type(exc).__name__}: {exc}".splitlines()[0]
    print(
        f"projects-orchestrator: internal error in '{command}': {detail} — this is a bug{hint}",
        file=sys.stderr,
    )
    return EXIT_INTERNAL_ERROR


def _stdout_is_broken() -> bool:
    """Whether the broken pipe is stdout's own, rather than some other pipe a command used.

    After a failed write, flushing stdout raises again only when stdout itself lost
    its reader. Any other ``BrokenPipeError`` (a subprocess's stdin, a socket) is a
    real fault and must be reported, not waved through as ``… | head``.
    """
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        # expected: this IS the answer — stdout's reader went away
        return True
    except (OSError, ValueError) as exc:
        _log.debug("cannot flush stdout: %r", exc)
    return False


def _silence_stdout() -> None:
    """At process exit, point fd 1 at /dev/null so the final flush cannot raise again.

    Registered with :mod:`atexit` rather than run in place, so a caller embedding
    ``main()`` keeps its stdout for as long as its process lives (Codex on #269).
    """
    # expected: stdout is no real descriptor (captured, or closed), so nothing is left to flush
    with contextlib.suppress(OSError, ValueError):
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, sys.stdout.fileno())
        finally:
            os.close(devnull)


def _native_windows() -> bool:
    """Report whether this is a native Windows shell (the shared platforms contract)."""
    return platform.system().startswith(_NATIVE_WINDOWS_PREFIXES)


def main(argv: list[str] | None = None) -> int:
    """Run the projects-orchestrator CLI.

    The engine degrades instead of raising (ADR-003), so an exception reaching
    this boundary is a bug. It exits :data:`EXIT_INTERNAL_ERROR` with one line
    rather than a traceback; ``--verbose`` adds the traceback back.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    if _native_windows():
        print(
            "projects-orchestrator: a native Windows shell is not supported. "
            "Run it inside WSL2 (macOS and Linux are supported; "
            "see the shared platforms contract).",
            file=sys.stderr,
        )
        return EXIT_UNSUPPORTED_PLATFORM
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    verbose = _verbose(args)
    with _diagnostic_trail(verbose):
        try:
            exit_code: int = args.handler(args)
            # Flush HERE, not at interpreter exit: a reader that went away
            # (`… | head`) otherwise surfaces after main() has returned, as
            # "Exception ignored" and exit 120, where nothing can classify it.
            sys.stdout.flush()
        except BrokenPipeError as exc:
            if not _stdout_is_broken():
                return _internal_error(args.command, exc, verbose)
            # The reader went away (`… | head`): nothing is wrong with the command.
            atexit.register(_silence_stdout)
            return 1
        except Exception as exc:  # noqa: BLE001 — the CLI boundary is where never-raise ends
            return _internal_error(args.command, exc, verbose)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

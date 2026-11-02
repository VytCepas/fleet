"""Read a child project's machine-readable self-description.

Every project scaffolded by project-init ships a ``config.yaml`` descriptor
(contract v1): name, language, tooling commands, memory tier and path. It lives
under ``.agents/`` on a current scaffold (project-init PI-627) and under
``.claude/`` on a legacy one — :func:`resolve_config` finds either. The
orchestrator is a *reader* of that contract — it never invents a parallel one.
Parsing never raises; malformed input degrades to defaults and is surfaced
through :attr:`ProjectDescriptor.warnings`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_log = logging.getLogger(__name__)

# Scaffold layout roots, most-current first. project-init PI-627 relocated the
# canonical tree from ``.claude/`` to ``.agents/`` and its ``.claude/`` projection
# deliberately EXCLUDES config.yaml/CAPABILITIES/memory (single source of truth),
# so a modern scaffold declares itself under ``.agents/``. Pre-PI-627 projects
# still ship ``.claude/config.yaml`` — read it as a legacy fallback.
_LAYOUT_DIRS: tuple[str, ...] = (".agents", ".claude")
_CONFIG_BASENAME = "config.yaml"

# Legacy alias kept for backward compatibility; new code resolves the layout via
# :func:`resolve_config` and reads paths off :attr:`ProjectDescriptor.config_root`.
CONFIG_RELPATH = Path(".claude") / _CONFIG_BASENAME


def resolve_config(project_dir: Path) -> tuple[Path, str] | None:
    """Locate a project's descriptor across scaffold layouts.

    A SYMLINKED marker is refused and the next layout is tried (marker contract
    cases M13 and M24, frozen by the ambient layer). ``is_file()`` follows
    symlinks, so a planted ``.agents`` — or an ``.agents/config.yaml`` — pointing
    anywhere on disk used to hand the fleet a descriptor authored outside the
    project's own review, and everything downstream is read from it: the safety
    allowlist, the hooks the scaffold is expected to carry, the observability
    path, the heal mode. A symlink is writable from outside the repo, which is
    exactly what makes it a forgery rather than a declaration. Refusing is the
    safe direction — an undiscovered project is visibly absent from the fleet,
    while a forged one is silently obeyed. project-init closed the same hole in
    its own walker in PI-903.

    Args:
        project_dir: Candidate project root.

    Returns:
        ``(config_path, config_root)`` — the readable ``config.yaml`` and the
        layout dir it lives in (``.agents`` preferred, ``.claude`` legacy) — or
        ``None`` when neither layout has a config.
    """
    for root in _LAYOUT_DIRS:
        layout = project_dir / root
        candidate = layout / _CONFIG_BASENAME
        try:
            if layout.is_symlink() or candidate.is_symlink():
                continue
            if candidate.is_file():
                return candidate, root
        except OSError as exc:
            # ADR-003: the engine never raises. These are stat calls, and a
            # stat raises for reasons that have nothing to do with this
            # project being malformed — a layout dir at mode 000, a dead
            # network mount, a permission-denied parent. ``load_descriptor``
            # already guards its own read; leaving the stats bare made the
            # guard cosmetic, because the raise happened one line earlier.
            #
            # The blast radius is what makes this a degradation and not a
            # nicety: ``registry.discover`` calls this inside its loop over
            # every candidate, so ONE unreadable directory aborted the loop
            # and emptied the whole fleet — healthy repos included, on every
            # verb, with ``doctor`` printing nothing at all. An undiscovered
            # project is visibly absent; an undiscovered FLEET looks like a
            # clean one.
            #
            # ``continue`` rather than ``return``: an unreadable ``.agents``
            # must not shadow a readable legacy ``.claude``, exactly as a
            # refused symlink does not.
            #
            # This is invisible to the suite's own interpreter. On CPython
            # 3.14 ``pathlib`` swallows EACCES and these calls return False;
            # on 3.13 — which is what a ``uv tool install`` of this package
            # pins — they raise. The regression tests therefore inject the
            # error rather than relying on chmod, or they would pass on a
            # tree where the bug is live (#210).
            _log.debug("cannot stat %s: %r", candidate, exc)
            continue
    return None


def layout_dir_present(project_dir: Path) -> str:
    """Name the scaffold layout a directory carries, or ``""``; never raises.

    The discriminator between a project that STOPPED RESOLVING and a directory
    that was never a project at all, which is the whole difficulty of warning
    about the first (#211). ``discover`` scans every directory one level under
    a root, so "warn whenever there is no descriptor" would fire on every
    ordinary folder beside the fleet — the §2.11 false positive that gets the
    warning switched off, taking the true positive with it.

    A layout directory is the evidence that something scaffolded this tree.
    Present with no readable ``config.yaml`` means the descriptor was deleted,
    replaced by a plain file, or made unreadable — all of which are worth a
    sentence. Absent means an ordinary directory, and silence is correct.

    Its LIMIT, stated because it is not obvious: removing ``.agents`` outright
    leaves nothing to distinguish the tree from any other folder, so that case
    stays silent unless the path is listed explicitly under ``projects:`` or is
    a git repo reached by ``include_plain_repos``.

    Args:
        project_dir: Candidate project root.

    Returns:
        The layout directory's name (``.agents`` or ``.claude``), or ``""``.
    """
    for root in _LAYOUT_DIRS:
        try:
            if (project_dir / root).exists():
                return root
        except OSError as exc:
            _log.debug("cannot stat %s: %r", project_dir / root, exc)
            continue
    return ""


def refused_symlink(project_dir: Path) -> str:
    """Name the symlinked marker :func:`resolve_config` refused, or ``""``; never raises.

    A refused symlink used to be reported exactly like an ordinary folder: "not a
    project-init project" when listed, "no readable config.yaml" when scanned
    (#220). Both read as absence, and the operator's fix for an absence (put a
    descriptor there) is already done — the descriptor IS there, one link away.
    Naming the refusal says which rule fired, so the fix is to replace the link
    with the file, not to go looking for a missing scaffold.

    Args:
        project_dir: Candidate project root.

    Returns:
        ``.agents``, ``.agents/config.yaml`` (or the ``.claude`` equivalents) for
        the first symlinked marker found, or ``""`` when there is none.
    """
    for root in _LAYOUT_DIRS:
        layout = project_dir / root
        for path, name in (
            (layout, root),
            (layout / _CONFIG_BASENAME, f"{root}/{_CONFIG_BASENAME}"),
        ):
            try:
                if path.is_symlink():
                    return name
            except OSError as exc:
                _log.debug("cannot stat %s: %r", path, exc)
    return ""


_TOOLING_SUFFIX = "_command"

CONTRACT_V1 = 1
CONTRACT_V2 = 2

DEPLOY_NONE = "none"

#: ``memory.stack`` value meaning the project declared NO memory backend. It is a
#: declaration, not an absence — ``unknown`` is the absence. The shipped ``core``
#: preset declares it the contract's way, by rendering no ``memory:`` block, so
#: :func:`_memory_stack` reads that absence as ``none`` at contract v1+ (#257).
MEMORY_STACK_NONE = "none"

# Memory tier at which each higher-tier retrieval surface first appears
# (project-init ADR-024 tier model, project-init ADR-025 §4). A child only
# *emits* the field at/above its tier, so the orchestrator reads it tier-gated:
# anchors never move, higher tiers only add surfaces, and a tier-0 read stays
# correct against a tier-3 child.
TIER_VAULT = 1
TIER_GRAPH = 2
TIER_RAG = 3

#: The recall ladder keyed by ``memory.stack``: project-init's ``_MEMORY_TIERS``
#: plus the permanent ``obsidian`` alias. The stack is the source of truth and
#: the tier is derived from it (project-init #960, #257 here). Gating on a
#: declared tier that contradicts its stack stripped surfaces with no signal: a
#: tier-2 child edited to ``tier: 0`` lost its graph and vault while ``doctor``
#: said ``[ok]``. Pinned to the vendored descriptor schema by
#: ``tests/check/fleet/test_contract.py``, so a producer change to the ladder fails here.
STACK_TIERS: dict[str, int] = {
    "auto": 0,
    "obsidian-only": 1,
    "obsidian": 1,
    "obsidian-graphify": 2,
    "obsidian-graphify-rag": 3,
}


@dataclass(frozen=True)
class DeployConfig:
    """Contract-v2 ``deploy:`` block for ``delivery: service`` projects.

    Attributes:
        target: Deploy target (``none`` | ``cloud-run`` | ``fly`` | ``k8s`` | …).
        app: App/service name at the target.
        region: Target region, when the platform needs one.
        health_url: HTTP health-check URL, empty when undeclared.
        workflow: The child's ``workflow_dispatch`` deploy pipeline the
            orchestrator triggers for cloud actions (ADR-005); empty falls back
            to the ``deploy.yml`` convention. The orchestrator never runs a
            platform mutation itself — it only dispatches this workflow.
    """

    target: str = DEPLOY_NONE
    app: str = ""
    region: str = ""
    health_url: str = ""
    workflow: str = ""


@dataclass(frozen=True)
class CiConfig:
    """Contract ``ci:`` block — a non-forge CI status endpoint (project-init #828).

    Optional and additive within contract v2: a child that omits it (every child
    scaffolded before project-init 1.1.7) is probed through its forge exactly as
    before. Feature-detected, per project-init ADR-025 §4 — not gated on a version bump.

    Attributes:
        status_url: JSON endpoint reporting the latest build; empty when the
            project's CI *is* the forge's and ``gh``/``glab`` should be used.
        status_field: Dot-path to the status value inside that JSON, for a
            response shape the auto-detection misses. Empty = auto-detect.
    """

    status_url: str = ""
    status_field: str = ""


@dataclass(frozen=True)
class ProjectDescriptor:
    """Everything the orchestrator knows about a project without running it.

    Attributes:
        name: Project name (directory name when the config omits it).
        path: Absolute path to the project root.
        language: Primary language declared at scaffold time.
        delivery: How the project ships (library | service | prototype).
        contract_version: Descriptor-contract schema version (0 when absent).
        project_init_version: Scaffold version the project was rendered with.
        project_init_plugin_version: The project-init plugin payload version
            the last upgrade installed (#212): the scaffold record's copy when
            present, else the visible ``project:`` field; ``unknown`` when absent.
        memory_tier: Memory tier (0 auto … 3 obsidian-graphify-rag), derived
            from ``memory_stack`` when the stack is on the ladder — a declared
            tier that disagrees is reported in ``warnings``, never obeyed (#257).
        memory_stack: Declared memory backend (``none`` | ``auto`` |
            ``obsidian-only`` | ``obsidian-graphify`` | ``obsidian-graphify-rag``);
            ``unknown`` when the config omits it. ``none`` is a real declaration
            and not an absence — the shipped ``core`` preset makes it by
            rendering no ``memory:`` block, read as ``none`` at contract v1+ —
            and the two must stay distinguishable, because a consumer that
            conflates them asks the operator to build what the project declined
            (#208, #257).
        memory_path: Absolute path to the project's memory directory.
        vault_path: Obsidian vault directory; ``None`` below tier 1 or when
            undeclared (higher-tier retrieval surface, project-init ADR-025 §4).
        graph_path: Graphify graph file; ``None`` below tier 2 or when
            undeclared.
        rag_endpoint: Tier-3 RAG query endpoint (URL or local address); empty
            below tier 3 or when the child has not run its RAG setup yet.
        tooling: Task name → shell command (lint, format, test, run, …).
        deploy: Contract-v2 deploy block; ``None`` below v2 or when absent.
        observability_path: Contract-v2 usage/guard-log directory; ``None``
            below v2 or when undeclared (callers fall back to convention).
        hooks_expected: Contract-v2 list of git hooks the scaffold ships;
            empty below v2 or when undeclared (callers fall back to globbing).
        host: Upstream forge host (``project.project_init_host``), e.g.
            ``github.com`` or ``gitlab.com``; empty when undeclared. Selects
            which forge adapter probes CI (``ci`` command).
        ci: Declared non-forge CI status endpoint; ``None`` when the child omits
            the block or leaves ``status_url`` empty — the overwhelmingly common
            case, in which the forge adapters probe CI as before.
        heal_mode: The project's declared heal-mode override (``fix`` |
            ``notify``); empty when undeclared, in which case the run-wide
            mode applies (ADR-008).
        context: Detect-and-defer boundary declaration (``repo`` | ``ambient``);
            empty when undeclared. ``repo`` — this project governs itself and an
            ambient agent layer stands down inside it. ``ambient`` — the owner
            opted out and the global layer keeps acting here. Empty means
            UNKNOWN and never ``ambient``: every descriptor scaffolded before
            project-init PI-901 lacks the key, so reading absence as an opt-out
            would un-declare the whole installed base at once. The fleet does
            not act on this — it manages a project either way — it reports it,
            so the value stops being a field three repos write and nobody reads
            (marker contract case H1).
        warnings: Human-readable parse problems, empty when the config is clean.
    """

    name: str
    path: Path
    config_root: str = ".claude"
    language: str = "unknown"
    delivery: str = "unknown"
    contract_version: int = 0
    project_init_version: str = "unknown"
    project_init_plugin_version: str = "unknown"
    memory_tier: int = 0
    memory_stack: str = "unknown"
    memory_path: Path | None = None
    vault_path: Path | None = None
    graph_path: Path | None = None
    rag_endpoint: str = ""
    tooling: dict[str, str] = field(default_factory=dict)
    deploy: DeployConfig | None = None
    observability_path: Path | None = None
    hooks_expected: tuple[str, ...] = ()
    host: str = ""
    ci: CiConfig | None = None
    heal_mode: str = ""
    context: str = ""
    #: Which marker key the config declares (``scope`` | ``context`` | ``both`` | ``""``), so the fleet can report
    #: the repos still on the legacy ``context:`` spelling.
    marker_key: str = ""
    warnings: tuple[str, ...] = ()
    #: Names of fields that were DECLARED but could not be read, so a consumer
    #: can tell "absent" from "present and broken" — which the coerced value
    #: alone cannot (#216). Absent fields are NOT listed here; nothing was
    #: declared, so nothing is malformed. Additive, per the `--json` seam's
    #: additive-only rule (the consumer's orchestrator-json contract).
    malformed: tuple[str, ...] = ()

    def has_task(self, task: str) -> bool:
        """Return whether the project declares a runnable command for ``task``."""
        return bool(self.tooling.get(task, "").strip())

    @property
    def config_path(self) -> Path:
        """Absolute path to the descriptor this project was read from."""
        return self.path / self.config_root / _CONFIG_BASENAME


def _as_mapping(value: Any) -> dict[str, Any]:
    """Return ``value`` if it is a mapping, else an empty dict."""
    return value if isinstance(value, dict) else {}


def contract_label(descriptor: ProjectDescriptor) -> str:
    """Render a descriptor's contract version for a human column (pure).

    ``bad`` when the field is DECLARED and unreadable, ``none`` when it is
    absent, ``v<n>`` otherwise — a negative included, because it is present and
    readable and ``none`` would deny that it exists.

    One definition. There were three renderings of this before #216 —
    ``fleet._contract_cell``, ``detail._summary_lines`` and ``doctor`` — and two
    of them had already diverged: a ``-3`` read as ``none`` in the fleet table
    and as ``v-3`` in the detail view, for the same project.
    """
    if "project_init_contract_version" in descriptor.malformed:
        return "bad"
    version = descriptor.contract_version
    return f"v{version}" if version != 0 else "none"


def _is_integral(value: Any) -> bool:
    """False for the values ``int()`` would reshape rather than read.

    ``int()`` raising is what makes a bad value visible, and two YAML scalars
    never raise. ``bool`` is a subclass of ``int``, so ``True`` returns ``1``;
    ``int()`` on a float truncates, so ``2.9`` returns ``2``. Both arrive back
    as a plausible number from a value the schema calls invalid
    (``descriptor.schema.json`` types both fields ``integer``), which is
    exactly the absent-vs-unreadable confusion #216 exists to end — worse here,
    because the number is not merely a default but a *wrong* answer that reads
    as declared. Raised as P2 on #228.

    An integral float stays admissible: JSON Schema counts ``2.0`` as an
    integer, so refusing it would invent a rule the contract does not have.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, float):
        return value.is_integer()
    return True


def _as_int(
    value: Any,
    default: int = 0,
    *,
    field: str = "",
    warnings: list[str] | None = None,
    malformed: list[str] | None = None,
) -> int:
    """Coerce ``value`` to int, falling back to ``default``.

    ABSENT and PRESENT-BUT-UNREADABLE both yield ``default``, and from the
    number alone no consumer can tell them apart. Measured before this warning
    existed (#216): a config declaring ``project_init_contract_version: "two"``
    read as ``0`` and ``doctor`` reported *"no project_init_contract_version —
    predates the contract"*, telling the operator to add a field that was
    already there. The ``status`` table's Contract column read ``none``,
    identical to true absence.

    Supplying ``field`` records the malformed case so a consumer can say which
    it is: ``warnings`` gets the human sentence, ``malformed`` gets the bare
    field name so a renderer can branch without matching on prose. A missing key
    and an explicit null are both treated as absent — nothing was declared, so
    there is nothing to complain about.

    A YAML bool or a fractional float is malformed, not coerced — see
    ``_is_integral``.

    NOT warned about: a coercible string such as ``"2"``. It is schema-invalid
    upstream and accepted here, one step less visible because the value happens
    to be right — but how many live repos quote the field is unmeasured, and a
    warning that fires across the fleet on a value that parses correctly is the
    §2.11 false positive that gets a control switched off. Left as a known gap
    rather than fixed blind.
    """
    if value is None:
        return default
    if _is_integral(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            # expected: reported below as a malformed field, with its value
            pass
    if field:
        if warnings is not None:
            warnings.append(f"{field} is not an integer — ignored: {value!r}")
        if malformed is not None:
            malformed.append(field)
    return default


def _extract_tooling(raw: dict[str, Any]) -> dict[str, str]:
    """Map ``<task>_command`` keys in the ``tooling`` block to task names."""
    tooling: dict[str, str] = {}
    for raw_key, value in _as_mapping(raw.get("tooling")).items():
        key = str(raw_key)
        if key.endswith(_TOOLING_SUFFIX) and isinstance(value, str) and value.strip():
            tooling[key.removesuffix(_TOOLING_SUFFIX)] = value.strip()
    return tooling


def _extract_deploy(raw: dict[str, Any]) -> DeployConfig | None:
    """Parse the v2 ``deploy:`` block; ``None`` when absent."""
    block = raw.get("deploy")
    if block is None:
        return None
    deploy = _as_mapping(block)
    return DeployConfig(
        target=str(deploy.get("target") or DEPLOY_NONE),
        app=str(deploy.get("app") or ""),
        region=str(deploy.get("region") or ""),
        health_url=str(deploy.get("health_url") or ""),
        workflow=str(deploy.get("workflow") or ""),
    )


def _extract_ci(raw: dict[str, Any]) -> CiConfig | None:
    """Parse the optional ``ci:`` block; ``None`` when absent or ``status_url`` is empty.

    An empty ``status_url`` is the scaffold default, and it means "my CI is the
    forge's" — so it collapses to ``None`` rather than an empty CiConfig, and
    callers can branch on presence alone.
    """
    block = _as_mapping(raw.get("ci"))
    status_url = str(block.get("status_url") or "").strip()
    if not status_url:
        return None
    return CiConfig(
        status_url=status_url,
        status_field=str(block.get("status_field") or "").strip(),
    )


#: Heal-mode values a child may declare (ADR-008): ``fix`` spawns the scoped
#: agent and lands a draft PR; ``notify`` reports the failure and spends nothing.
HEAL_MODES = ("fix", "notify")

# Detect-and-defer boundary values, frozen in the marker contract and mirrored
# by project-init's descriptor schema enum (marker contract case H1).
CONTEXT_VALUES = ("repo", "ambient")
#: The standard's spelling of the same declaration (D-12): ``scope: project`` is ``repo``, ``scope: user`` is ``ambient``.
SCOPE_VALUES = {"project": "repo", "user": "ambient"}


def _plugin_version(raw: dict[str, Any], project: dict[str, Any]) -> str:
    """The plugin version the last ``project-init upgrade`` installed (#212).

    Two copies exist. ``scaffold.variables`` is the record ``upgrade`` has always
    rewritten. The visible ``project:`` field was frozen at render time until
    project-init#1019, so a repo not upgraded since then shows a stale value:
    measured 2026-09-19, 6 of 7 governed repos disagreed, one reading 0.1.0
    against a recorded 0.9.16. The record wins when present, so ``upgrade-plan``
    compares what is actually installed. Since #1019 one value writes both.
    """
    scaffold = raw.get("scaffold")
    variables = scaffold.get("variables") if isinstance(scaffold, dict) else None
    recorded = variables.get("project_init_plugin_version") if isinstance(variables, dict) else None
    if isinstance(recorded, str) and recorded.strip():
        return recorded.strip()
    return str(project.get("project_init_plugin_version") or "unknown")


def _extract_heal_mode(raw: dict[str, Any], warnings: list[str]) -> str:
    """Parse the optional ``heal.mode`` override; ``""`` when absent.

    Feature-detected like ``ci``, not version-gated. An unknown value is
    ignored WITH a warning rather than obeyed or guessed: a typo'd mode must
    not silently switch a project between "spends money and opens PRs" and
    "tells me and stops".
    """
    mode = str(_as_mapping(raw.get("heal")).get("mode") or "").strip()
    if not mode:
        return ""
    if mode not in HEAL_MODES:
        warnings.append(f"heal.mode '{mode}' is not one of {'|'.join(HEAL_MODES)} — ignored")
        return ""
    return mode


def _read_marker_key(
    raw: dict[str, Any], key: str, allowed: tuple[str, ...], warnings: list[str]
) -> str:
    """One marker key's value, or ``""`` when absent, empty or not one of *allowed*."""
    if key not in raw:
        return ""
    value = raw.get(key)
    if value is None:
        # `key:` with nothing after it. YAML gives None, and that is the one
        # shape genuinely indistinguishable from an absent key — the writer got
        # as far as the key and no further.
        return ""
    # PRESENT BUT NOT A STRING is malformed, not absent (PR #200 review). `context:
    # true`, `123`, a list or a mapping used to be coerced to "" and returned
    # silently, which made an invalid boundary marker read exactly like a
    # deliberate omission — and absence has a defined meaning here (M21: fall back
    # to marker presence). A reader that cannot tell them apart hands the operator
    # a repo that looks unmarked and is actually mis-marked.
    if not isinstance(value, str) or not value.strip():
        warnings.append(f"{key} must be one of {'|'.join(allowed)} — ignored: {value!r}")
        return ""
    text = value.strip()
    if text not in allowed:
        warnings.append(f"{key} '{text}' is not one of {'|'.join(allowed)} — ignored")
        return ""
    return text


def _extract_context(raw: dict[str, Any], warnings: list[str]) -> str:
    """Parse the top-level boundary marker, either spelling; ``""`` when absent.

    The standard's key is ``scope: project|user`` (D-12); ``context: repo|ambient``
    is the legacy spelling and is still read (#966, #963). They map
    ``project`` = ``repo`` and ``user`` = ``ambient``, and EITHER key opting out
    decides ``ambient``: a config carrying ``context: repo`` beside ``scope: user``
    is ``ambient``, because for the guards that read this field the opt-out is the
    safe direction (marker contract case M40).

    The value is TOP-LEVEL by contract, which YAML gives for free here: a
    key indented under some other block parses as that block's key and
    is never seen, and a commented-out one is not a key at all. Both are rules
    the shell and regex readers of this same field have to spell out.

    An unknown value is ignored WITH a warning rather than guessed. For a key
    three implementations in two languages read, the realistic failure is a
    near-miss (``Repo``, ``project`` under ``context``) that looks marked to a
    human and resolves to nothing in code — project-init's descriptor schema
    constrains it to an enum for the same reason.
    """
    legacy = _read_marker_key(raw, "context", CONTEXT_VALUES, warnings)
    scope = _read_marker_key(raw, "scope", tuple(SCOPE_VALUES), warnings)
    verdicts = [v for v in (legacy, SCOPE_VALUES.get(scope, "")) if v]
    if "ambient" in verdicts:
        return "ambient"
    return verdicts[0] if verdicts else ""


def marker_keys(raw: dict[str, Any]) -> str:
    """Which marker keys a config declares: ``scope``, ``context``, ``both`` or ``""``."""
    has_scope = raw.get("scope") is not None
    has_context = raw.get("context") is not None
    if has_scope and has_context:
        return "both"
    return "scope" if has_scope else ("context" if has_context else "")


ESCAPES = "escapes the project root"
UNRESOLVABLE = "cannot be resolved"


def _contain(project_dir: Path, relative: str) -> Path | str:
    """Join ``relative`` under ``project_dir``, or say why it cannot be used.

    A descriptor is data the orchestrator only reads, but a ``memory_path`` or
    ``observability.path`` of ``../../etc`` or ``/etc`` would resolve outside
    the project root (``Path('/proj') / '/etc'`` is ``/etc``). Reject any value
    whose resolved location is not the project dir or beneath it; contained
    values keep their plain (unresolved) join so callers compare cleanly.

    A path that cannot be resolved at all cannot be shown to be contained, so it
    is rejected too, with its own reason. A symlink loop raises ``RuntimeError``
    on Python 3.11/3.12; a YAML escape that decodes to a NUL raises
    ``ValueError``, and one that decodes to a lone surrogate raises
    ``UnicodeEncodeError``, a ``ValueError``. Raising here would abort discovery
    of the whole fleet over one project's descriptor (PR #262 review).

    Returns:
        The contained path, or the reason it was rejected: :data:`ESCAPES` or
        :data:`UNRESOLVABLE`, worded to complete a warning sentence.
    """
    try:
        resolved = (project_dir / relative).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        _log.debug("cannot resolve %r under %s: %r", relative, project_dir, exc)
        return UNRESOLVABLE
    if resolved == project_dir or project_dir in resolved.parents:
        return project_dir / relative
    return ESCAPES


def _extract_observability_path(
    raw: dict[str, Any], project_dir: Path, warnings: list[str]
) -> Path | None:
    """Resolve the v2 ``observability.path``; ``None`` when undeclared/escaping."""
    declared = _as_mapping(raw.get("observability")).get("path")
    if not isinstance(declared, str) or not declared.strip():
        return None
    contained = _contain(project_dir, declared.strip())
    if isinstance(contained, str):
        warnings.append(f"observability.path '{declared.strip()}' {contained} — ignored")
        return None
    return contained


@dataclass(frozen=True)
class _MemorySurface:
    """The parsed ``memory:`` block plus the context tier-gating needs."""

    block: dict[str, Any]
    tier: int
    project_dir: Path


def _tier_gated_path(
    memory: _MemorySurface, key: str, min_tier: int, warnings: list[str]
) -> Path | None:
    """Resolve a tier-gated memory path (``vault_path``/``graph_path``).

    Read only at/above ``min_tier`` — a lower-tier child never emits it, and
    ignoring a stray value keeps the anchors-never-move invariant (a value that
    only appears with its tier can never shift a lower-tier reader's behaviour).
    Ignored is not the same as unreported, though (#236): a value declared below
    its gate is dropped WITH a warning, the same channel an escaping path uses.
    Dropping it silently made the one descriptor fault this reader said nothing
    about look like a project that never declared the surface.

    Containment is checked BEFORE the gate, so an escaping or unresolvable
    path warns as such at any tier. Gating first let a below-gate escape through both
    checks without a word.
    """
    declared = memory.block.get(key)
    if not isinstance(declared, str) or not declared.strip():
        return None
    contained = _contain(memory.project_dir, declared.strip())
    if isinstance(contained, str):
        warnings.append(f"memory.{key} '{declared.strip()}' {contained} — ignored")
        return None
    if memory.tier < min_tier:
        warnings.append(_below_gate(key, memory.tier, min_tier))
        return None
    return contained


def _below_gate(key: str, tier: int, min_tier: int) -> str:
    """The warning for a retrieval surface declared below its memory-tier gate."""
    return f"memory.{key} is declared at memory tier {tier} but needs tier {min_tier}+ — ignored"


def _tier_gated_endpoint(memory: _MemorySurface, warnings: list[str]) -> str:
    """Resolve the tier-3 ``rag_endpoint`` string; empty below tier 3/undeclared.

    Unlike the vault/graph *paths*, the endpoint is an opaque address (a URL or
    ``host:port``), so it is kept as a plain string rather than a contained path.
    A non-empty endpoint below tier 3 is dropped with a warning, as the paths
    are (#236).
    """
    endpoint = memory.block.get("rag_endpoint")
    value = endpoint.strip() if isinstance(endpoint, str) else ""
    if value and memory.tier < TIER_RAG:
        warnings.append(_below_gate("rag_endpoint", memory.tier, TIER_RAG))
        return ""
    return value


def _memory_stack(raw: dict[str, Any], memory: dict[str, Any], contract_version: int) -> str:
    """The declared memory backend, reading an absent block under the contract.

    project-init renders the ``memory:`` block only when memory is on, so under
    the descriptor contract (v1+) a config with NO block declared ``none``: the
    producer's own reader rule says so. Reading that absence as ``unknown`` made
    every real ``core`` scaffold look like a project that forgot its memory, and
    the #208 guard in ``hardening`` — keyed on a declared ``none`` — never fired
    on one, because no scaffold writes ``stack: none`` into a block (#257,
    project-init #960/#964). A v0 config predates the rule, so there absence
    stays ``unknown``; so does a present block that omits ``stack``.
    """
    if "memory" not in raw and contract_version >= CONTRACT_V1:
        return MEMORY_STACK_NONE
    return str(memory.get("stack") or "unknown")


def _memory_tier(
    memory: dict[str, Any], stack: str, warnings: list[str], malformed: list[str]
) -> int:
    """The tier to gate retrieval surfaces on: the stack's, never a contradiction.

    ``memory.tier`` gates which surfaces are read at all, so a silent coercion
    to 0 downgrades a tier-3 project to flat files with no signal (#216); a
    malformed value is recorded exactly as before. What changed (#257): for a
    stack on the ladder the tier is DERIVED, and a declared tier that disagrees
    is reported rather than obeyed. An absent or unrecognised stack leaves the
    declared tier as the only evidence, which is how every legacy config read.
    ``none`` declares memory declined: no rung, so no surface is read.
    """
    declared_raw = memory.get("tier")
    declared = _as_int(declared_raw, field="memory.tier", warnings=warnings, malformed=malformed)
    if stack == MEMORY_STACK_NONE:
        return 0
    derived = STACK_TIERS.get(stack)
    if derived is None:
        return declared
    if declared_raw is not None and "memory.tier" not in malformed and declared != derived:
        warnings.append(
            f"memory.tier {declared} disagrees with memory.stack '{stack}' (tier {derived}) — "
            f"reading tier {derived}; the stack is the source of truth"
        )
    return derived


def _extract_hooks_expected(raw: dict[str, Any]) -> tuple[str, ...]:
    """Parse the v2 ``hooks.expected`` list; empty when undeclared."""
    expected = _as_mapping(raw.get("hooks")).get("expected")
    if not isinstance(expected, list):
        return ()
    return tuple(str(name) for name in expected if isinstance(name, str) and name.strip())


def _printable(text: str) -> str:
    """Escape the characters of ``text`` that a terminal cannot be handed.

    A warning quotes the declared value, and a YAML escape can declare a NUL or
    a lone surrogate. A surrogate makes ``print`` raise ``UnicodeEncodeError`` on
    a UTF-8 stream, so the non-JSON ``doctor`` and ``audit`` paths would crash
    while reporting the very descriptor fault they found (PR #262 review).
    Printable text, non-ASCII included, is kept as written.
    """
    return "".join(
        c if c.isprintable() else c.encode("unicode_escape").decode("ascii") for c in text
    )


def parse_config(text: str, project_dir: Path, config_root: str = ".claude") -> ProjectDescriptor:
    """Build a descriptor from raw config text (pure; never raises).

    Args:
        text: Contents of the project's ``config.yaml``.
        project_dir: Project root the config belongs to.
        config_root: Layout dir the config was found in (``.agents`` for a
            PI-627 scaffold, ``.claude`` legacy). Read surfaces without an
            explicit path in the config (memory, capabilities, observability)
            default under this dir. Defaults to ``.claude`` for direct callers.

    Returns:
        A descriptor; parse failures degrade to defaults with a warning.
        Contract-v2 surfaces (deploy, observability, hooks) are parsed only
        when the config declares ``project_init_contract_version >= 2`` —
        additive fields on a v1 config are ignored, exactly as a v1 reader
        would ignore them.
    """
    project_dir = project_dir.resolve()
    warnings: list[str] = []
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raw = None
        warnings.append(f"config.yaml is not valid YAML: {exc}".splitlines()[0])
    raw = _as_mapping(raw)
    if not raw and not warnings:
        warnings.append("config.yaml is empty")

    project = _as_mapping(raw.get("project"))
    memory = _as_mapping(raw.get("memory"))
    memory_default = f"{config_root}/memory"
    memory_rel = str(memory.get("memory_path") or memory_default)
    contained = _contain(project_dir, memory_rel)
    if isinstance(contained, str):
        warnings.append(f"memory_path '{memory_rel}' {contained} — using {memory_default}")
        contained = project_dir / memory_default
    memory_path = contained
    malformed: list[str] = []
    contract_version = _as_int(
        project.get("project_init_contract_version"),
        field="project_init_contract_version",
        warnings=warnings,
        malformed=malformed,
    )
    is_v2 = contract_version >= CONTRACT_V2
    memory_stack = _memory_stack(raw, memory, contract_version)
    memory_tier = _memory_tier(memory, memory_stack, warnings, malformed)
    surface = _MemorySurface(block=memory, tier=memory_tier, project_dir=project_dir)

    return ProjectDescriptor(
        name=str(project.get("name") or project_dir.name),
        path=project_dir,
        config_root=config_root,
        language=str(raw.get("language") or "unknown"),
        delivery=str(raw.get("delivery") or "unknown"),
        contract_version=contract_version,
        project_init_version=str(project.get("project_init_version") or "unknown"),
        project_init_plugin_version=_plugin_version(raw, project),
        memory_tier=memory_tier,
        memory_stack=memory_stack,
        memory_path=memory_path,
        vault_path=_tier_gated_path(surface, "vault_path", TIER_VAULT, warnings),
        graph_path=_tier_gated_path(surface, "graph_path", TIER_GRAPH, warnings),
        rag_endpoint=_tier_gated_endpoint(surface, warnings),
        tooling=_extract_tooling(raw),
        deploy=_extract_deploy(raw) if is_v2 else None,
        observability_path=(
            _extract_observability_path(raw, project_dir, warnings) if is_v2 else None
        ),
        hooks_expected=_extract_hooks_expected(raw) if is_v2 else (),
        host=str(project.get("project_init_host") or ""),
        # Feature-detected, NOT version-gated (unlike deploy/observability/hooks,
        # which arrived *with* v2). `ci` is an additive field within v2, so the
        # contract version says nothing about whether a child emits it — project-init ADR-025
        # §4's rule is to detect the surface, not infer it from a version. A v1
        # child that hand-adds the block is honoured too, which costs nothing.
        ci=_extract_ci(raw),
        heal_mode=_extract_heal_mode(raw, warnings),
        context=_extract_context(raw, warnings),
        marker_key=marker_keys(raw),
        warnings=tuple(_printable(w) for w in warnings),
        malformed=tuple(malformed),
    )


def parse_scaffold_version(value: str) -> tuple[int, int, int] | None:
    """Parse a ``MAJOR.MINOR.PATCH`` scaffold version into a comparable tuple.

    Args:
        value: A ``project_init_version`` string.

    Returns:
        The three numeric components, or ``None`` when the value is missing,
        ``unknown``, or not exactly three integer components. Malformed shapes
        (``0.6``, ``999``, ``1.2.beta``) degrade to "not comparable" rather
        than a misleading order that could mark valid projects as behind.
    """
    if not value or value == "unknown":
        return None
    parts = value.split(".")
    if len(parts) != 3:
        return None
    try:
        major, minor, patch = (int(part) for part in parts)
    except ValueError:
        # expected: an unparseable version IS the answer: None means not comparable
        return None
    return (major, minor, patch)


def load_descriptor(project_dir: Path) -> ProjectDescriptor | None:
    """Load the descriptor for one project directory.

    Args:
        project_dir: Candidate project root.

    Returns:
        The parsed descriptor, or ``None`` when the directory is not a
        project-init project (no readable ``.agents/config.yaml`` or legacy
        ``.claude/config.yaml``).
    """
    resolved = resolve_config(project_dir)
    if resolved is None:
        return None
    config_path, config_root = resolved
    try:
        # errors="replace": a config saved in a non-UTF-8 encoding degrades to
        # a slightly-garbled descriptor rather than dropping the project from
        # discovery entirely (the engine never raises — ADR-003).
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _log.debug("cannot read %s: %r", config_path, exc)
        return None
    return parse_config(text, project_dir, config_root)

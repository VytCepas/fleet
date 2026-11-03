# Changelog

Notable changes to the `projects-orchestrator` tool itself. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

A release moves the `Unreleased` entries under a new version heading, and that
heading must equal `__version__` in `src/projects_orchestrator/__init__.py`, the
version's only source. `tests/test_version.py` fails when they disagree.

## [Unreleased]

### Added

- **The audit holds a project's justfile to the task verbs its repo-kind requires** (category
  `verbs`, control `check.repo-verbs`, standard#1428). The project declares `repo-kind:` in
  `.agents/config.yaml`; its vendored `lint-name.sh --check repo-kind` reports each missing verb
  of the standard's `verbs.tsv` as `name.repo-verb`, and a `naming-exemptions.tsv` row silences
  one. A project that declares no kind is not judged. This repo declares `fleet-controller` and
  gains the `naming` recipe it lacked.
- **A decision series, `docs/decisions/`, starting at D-1** (standard#1429), rendered from the
  scaffolder's record template. Records are cited `D-<n>` here and `fleet/D-<n>` from any other
  repo; the ADRs under `.agents/docs/adr/` predate it and keep their numbers.

### Changed

- **`watch` emits alerts and never pushes them: `--webhook` and `PO_WATCH_WEBHOOK` are
  removed.** Nothing scheduled `watch`, and a pass that could post to a webhook was a
  second notifier beside the scheduler's own. The pass keeps the text and `--json`
  report and its exit code (1 eventful, 0 quiet, 2 no fleet); the scheduler that runs it
  turns those into its notice. `notify --webhook` is unchanged.

- **`just install` fetches `origin/main` in its dry run, and a refusal prints as
  `  FAIL: <reason>` then `  fix: <command>`**. The dry run judged "in sync" by
  the last-fetched ref and passed a clone five merges behind, which `--apply` then
  refused. Both now fetch and agree; a failed fetch is a refusal. The fix lines
  are commands, such as `git -C <checkout> pull --ff-only origin main`.
- **`heal` runs on `haiku` at high effort, falling back to `sonnet`.** A heal is a
  narrow fix that its own gate re-checks. In a 141-run trial of scoped, gate-checked
  tasks, Haiku at high effort passed as reliably as Sonnet and Opus at about a fifth
  of Sonnet's cost per passed task. `work` stays on `sonnet` until its run records
  show how Haiku does on real tickets. Override under `models:` in `fleet.yaml`.

### Added

- **A model tier and effort per task class** (#324). Every unattended
  `claude` this tool starts runs under a fresh HOME, so it took the CLI default
  model. Each launcher now passes `--model`, `--effort` and `--fallback-model`,
  read from one table (`models.py`, overridable under `models:` in `fleet.yaml`):
  `heal` and `work` on `sonnet`, and `attach` (the interactive session a human
  takes over a failed run in) on `opus`. Only the aliases `haiku`, `sonnet` and
  `opus` are accepted; a pinned model id, an unknown effort or a fallback equal to
  its model is refused at load with a warning, and the defaults stand. A `work` run
  records its `model`, `effort` and `fallback_model` so the mix can be measured;
  older records still load.
- `just install`, the one install route for the tool itself. With no flag it is
  a dry run that prints the source, commit, target and command, and what
  `--apply` would refuse; it exits 1 when `--apply` would refuse, unless the
  only reason is a Claude Code session. `--apply` runs `uv tool install --reinstall` on the
  checkout, only from the main worktree on a clean `main` in sync with
  `origin/main`, then checks the result. `--check` compares the installed package
  files and their executable bits with `HEAD` and exits 1 on drift, naming each file, so a pull without a
  reinstall is no longer invisible. It also fails on installed metadata that
  differs from `HEAD`'s `pyproject.toml`, and on a `projects-orchestrator` earlier
  on `PATH` that shadows the tool (#317).

### Changed

- The fleet root defaults to `PORT_ROOT`, not the checkout's parent. With no
  `--fleet`, no `--root`, no `fleet.yaml` in the working directory and no
  `PO_FLEET_ROOT`, discovery scans `$PORT_ROOT`, else `~/port` — the directory
  the repositories live in side by side, and the same one from any working
  directory. An empty `PORT_ROOT` counts as unset; with neither it nor `HOME`
  set there is no default, and discovery says so instead of scanning `/port`.
  The systemd units default `--root` the same way instead of `~/projects`.
  **Breaking** for a fleet that relied on either old default: export
  `PO_FLEET_ROOT` (or `PORT_ROOT`) to the directory it lives in (#313).

### Fixed

- Every GitHub write heal makes — the draft PR, and notify mode's issues —
  names the repository its `origin` remote points at. `gh` resolves a clone
  with a second remote to `upstream`, so a fork clone would have opened PRs
  and filed issues on the upstream repository. The remote's host travels with
  the answer, so GHE.com and GHES children keep working (project-init ADR-013),
  and an `origin` that `gh` cannot be pointed at is refused rather than left to
  `gh`'s own resolution (#286).

## [0.3.0] - 2026-09-19

The first release since 0.2.0: the fleet-audit fixes, the frozen `--json`
seam, heal notifications (webhook and notify-mode issues), and the first build
meant for PyPI.

### Added

- `upgrade-plan` reports each project's plugin payload against the version
  project-init's default branch ships: `plugin 0.9.16 → 0.9.20 behind`, `ok`,
  or `unknown`, never `ok` when upstream cannot be read. It is its own field;
  the scaffold status and the exit code are unchanged (#212).
- The scheduled contract-freshness check fails when project-init declares a
  descriptor contract version newer than `doctor.CONTRACT_VERSION_MAX`, so the
  lock-step rule goes red once, before any child upgrades. `doctor` still
  warns (#221).
- `--json` output is a frozen seam: versioned schemas under `schemas/`, golden
  fixtures, and producer-side validation (#230).
- `watch` reports when its own timer stops, instead of going quiet (#244).
- The never-raise contract is tested module by module (#243). The nightly fuzz
  gate now explores inputs rather than passing on none (#245).
- `tech-debt` and `spike` are first-class issue types (#250).
- `--verbose` (or `PROJECTS_ORCHESTRATOR_VERBOSE=1`) logs every error a
  degraded path swallowed, and `upgrade-plan` names the reason for each
  `unknown` row. An unexpected internal error exits 70 with one line instead of
  a traceback (#269).
- A scheduled heal posts what it did, or why it failed, to `PO_HEAL_WEBHOOK`.
  A clean pass posts nothing, and the healed PR stays a draft (#276).
- The integration job runs real tests: the installed CLI over real git
  repositories (#274).
- `fleet.yaml` can list `memory_sources`: extra memory directories that
  `memory` search reads and ranks with the projects' own. A missing or
  unreadable one is warned about (#247).
- `fleet.yaml` can declare a `host_health_command`. Its first output line is
  a host-health tile under the `status` and `snapshot` tables, on both
  dashboards and in the TUI header. It reads `host: unknown` when absent,
  failing, slow or silent (#247).
- `heal --issues` (`PO_HEAL_ISSUES=1` on the timer) files one GitHub issue per
  failing gate of each notify-mode project, on that project's repository, and
  closes it once the gate passes. Issues are deduplicated by a marker in their
  body, not a state file (#164).

### Changed

- The version is single-sourced from `__init__.py` (#191).
- `memory` search ranks hits by BM25 and matches query terms separately, rather
  than scoring every substring hit alike (#264).
- Fleet discovery no longer counts a linked worktree as a separate project when
  its repository is already in the fleet (#261).
- The memory tier is derived from `memory.stack`. A contract-v1+ descriptor with
  no `memory:` block reads as memory declined (#258).
- Every cache and registry write goes through one hardened write path, which
  closes a registry race (#240). A cache written by a newer build is no longer
  read as empty and overwritten (#242).
- Every `gcloud` subprocess is pinned to a named identity rather than the ambient
  account.
- The scaffold is re-rendered from current project-init, taking its upstream
  security fixes (#202, #226, #233).

### Fixed

- `--json` exits exactly as the text mode does. Ten commands (`checks`, `drift`,
  `doctor`, `audit` and `audit --digest`, `hardening`, `ci`, `cloud-status`,
  `upgrade-plan`, `register`) exited 0 under `--json` whatever they found (#278).
- An unresolved fleet reported clean and exited 0 (#239).
- One unreadable `.agents` directory blacked out the whole fleet (#234). A
  scanned project that stopped resolving dropped out silently (#235). A project
  nested more than one level deep was invisible (#227).
- A memory file in the documented format was typed `unknown` (#229).
- `doctor` named the wrong problem for a malformed contract version (#228), and
  hardening told a `memory: none` project to build memory (#238).
- Hook health read `missing` on every worktree while the hooks fired (#223).
- The review gate could not see a review that arrived as a comment (#231).
- The `work` briefing promised a gate re-run that `work` never runs (#256).
- Four Linux-only tests failed on macOS (#206). `PO_FLEET_ROOT` is read, and
  discovery says where it looked when it finds nothing (#207).
- The golden fixtures are regenerated with project-init 1.2.2 (#259).
- A retrieval surface declared below its memory-tier gate was dropped without a
  warning, and a descriptor path that could not be resolved aborted discovery
  of the whole fleet (#262).
- `doctor` called an explicit contract version of `0` absent (#265).
- An installed git hook older than its tracked source read `ok`. It reads
  `stale` now, and `doctor`, the hardening checklist and `watch` report it (#272).
- The URL guard let the IPv4-mapped cloud metadata address through on CPython
  3.12.3. It now unwraps the mapped address itself. The nightly mutation job
  copies every file the tests read (#273).
- A descriptor refused because it is a symlink was reported as "not a
  project-init project". The warning now names the refused link (#220).
- A project that declares `heal.mode: notify` was advised to run `heal <project>`,
  which its own declaration overrides. The advice now says the declaration has
  to change first.
- The notify-mode issue sink (#282):
  - a gate that printed a NUL byte crashed the heal pass;
  - `PO_HEAL_ISSUES=0` turned filing on;
  - an unreadable issue list on a clean pass reported `delivery failed`;
  - issues were filed and closed on uncommitted working-copy state.
  It now files and closes only on results taken at a clean checkout of the
  commit `origin`'s default branch points at, so a fix on an unpushed local
  branch no longer closes the issue.
- `upgrade-plan` showed a repo's frozen visible plugin version (0.1.0 on one
  repo) rather than the one its last upgrade recorded (0.9.16). The recorded
  version now wins when present.

### Security

- Descriptor-declared URLs pass a scheme and host allowlist (#225).
- A symlinked descriptor is refused (#200). A redirection bypass is closed, and
  the projection no longer deletes unmanaged files (#203).
- The Read-deny rules cover what the Bash guard already covers (#224).
- A test fails when the shipped or tracked tree names a private upstream
  repository; the shipped set is read from `pyproject.toml` (#267).

### Documentation

- The README documents every registered command, and a test keeps it that way
  (#246).
- Where the fleet's schedulers run, and that nothing schedules them on macOS
  (#252).
- Installing and upgrading the tool itself:
  [docs/how-to/install-and-upgrade.md](docs/how-to/install-and-upgrade.md)
  (#191).
- ADR citations say which repository owns the ADR, and a test fails on a
  citation that resolves to no ADR (#266).
- The descriptor contract no longer claims `config.yaml` is hash-covered (#275).

## [0.2.0] - 2026-07-13

The first release: the fleet engine (descriptor registry, checks, drift, memory
search, controller, TUI) and the project-init scaffold. Full notes:
[GitHub release v0.2.0](https://github.com/VytCepas/projects-orchestrator/releases/tag/v0.2.0).

[Unreleased]: https://github.com/VytCepas/projects-orchestrator/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/VytCepas/projects-orchestrator/releases/tag/v0.3.0
[0.2.0]: https://github.com/VytCepas/projects-orchestrator/releases/tag/v0.2.0

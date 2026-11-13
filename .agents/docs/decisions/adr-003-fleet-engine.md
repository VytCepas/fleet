# ADR-003: Fleet Engine — Descriptor Reader, Never-Raise Core, Deterministic Controller

**Date:** 2026-07-02
**Status:** Accepted

## Context

The package was a CLI stub while the repo's purpose is a root orchestrator:
one controllable interface that monitors and drives every child project.
Child projects scaffolded by project-init already ship a machine-readable
self-description (`.claude/config.yaml`, descriptor contract v1: name,
language, tooling commands, memory tier/path) and a structured memory
directory (`.claude/memory/*.md` with name/description/type frontmatter,
indexed by `MEMORY.md`). The orchestrator needed a foundation that reads
those contracts across many projects and stays usable when any child is
broken, missing, or half-scaffolded.

## Decision

1. **The orchestrator is a contract reader, not a contract author.**
   `descriptor.py` parses contract v1 as-is; `memory.py` reads the existing
   memory schema. No parallel metadata format is introduced.
2. **Never-raise engine.** Every module that touches the outside world
   (subprocess, filesystem, YAML) returns degraded data instead of raising:
   unparseable config → defaults + `warnings`; non-git dir → health
   `unknown`; failing/missing/timed-out gate → a `fail` CheckResult. All
   subprocess calls go through one timeout-bounded runner (`runner.py`).
   The fleet view must render no matter what state a child is in.
3. **Gates are the child's own.** `checks.py` runs only the commands a
   project declares in `tooling.*_command`; an undeclared gate is `skip`,
   never a guessed command.
4. **Last-known results persist.** `cache.py` stores check results per
   `(project, task)` under `$XDG_CACHE_HOME/projects-orchestrator/` so the
   fleet table answers "does it pass, and how fresh is that?" without
   re-running everything. A corrupt cache reads as empty.
5. **Drift and hook health are first-class parameters.** `drift.py`
   compares each child's tree against the `scaffold.manifest` SHA-256 map
   project-init records, and checks that `.github/hooks/*` are actually
   installed in `.git/hooks/` — because enforcement that isn't installed
   never runs. Both work offline and degrade (`no-manifest`, `-`).
6. **One truth, many surfaces.** `fleet.py` joins descriptor + git status +
   cached checks + memory into `ProjectSnapshot`; pure `fleet_rows()` feeds
   the text table, `--json`, and the Textual TUI identically.
6. **Deterministic controller, LLM-free.** `controller.py` maps text to a
   typed `Intent` (pure `parse_command`) and dispatches to the engine.
   `/ask` is a seam for an optional natural-language mode that may only
   *select among existing intents*.

   *Since accepted:* that seam is now filled (`ask.py`), and the constraint
   held — `/ask` resolves a question to an **existing** intent and the
   deterministic dispatcher still executes it. It is opt-in and off by
   default (`ORCHESTRATOR_ASK_MODEL` + `ANTHROPIC_API_KEY`), and it may
   *propose* a `work` run but is structurally unable to launch one (#124).
7. **Dependencies:** `pyyaml` (runtime) for robust contract parsing;
   `textual` only as the optional `tui` extra so the core stays light.

## Consequences

- `projects-orchestrator {projects,status,checks,memory,snapshot,controller,tui}`
  is the single control surface; external monitors consume `--json`.
- Fleet membership comes from `fleet.yaml` (roots/projects/exclude) or the
  sibling-directory convention — adding a project is dropping it next to
  the others. (Amended by #313: "next to the others" is `$PORT_ROOT`, else
  `~/port`, rather than the parent of whichever checkout the command runs
  from — the same directory from every working directory.)
- Engine logic stays UI-free and unit-tested with real subprocesses/git
  repos (no mocks), per repo test conventions.
- Cross-repo features (CI status via `gh`, board aggregation, fleet-wide
  upgrades) can build on `ProjectSnapshot` without touching the core.

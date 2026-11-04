# projects-orchestrator

Cross-project orchestration layer for agentic development

Canonical instructions for **all** coding agents (and humans pairing with
them). Claude Code reads [CLAUDE.md](CLAUDE.md), which redirects here.

## Start here

Agent-neutral infrastructure lives under [`.agents/`](.agents/) — the
directory name is historical; the contents (bash lifecycle scripts, markdown
skills, memory, and docs) are readable by any agent. Harness-specific wiring is
*not* kept here: each surface you enable with `--agents` gets its own generated
config in its own directory (`.codex/`, `.agents/`, `example:.cursor/`, `example:.amp/`,
`example:.junie/`, `.vscode/`, plus root `.mcp.json`). Within `.agents/`, only Claude
Code's own hook configuration is Claude-specific (see "Claude Code specifics"
below); everything else is reusable by any agent.

- [`docs/explanation/overview.md`](docs/explanation/overview.md) — the whole system on one page: capabilities, the read-only project-init contract boundary, and the architecture (read this for the mental model)
- [`.agents/project-init.md`](.agents/project-init.md) — workflow, conventions, task tracking
- [`.agents/memory/MEMORY.md`](.agents/memory/MEMORY.md) — memory index (read first for context)
- [`.agents/docs/`](.agents/docs/) — system of record: ADRs and development guides
- `.agents/docs/CODE_MAP.md` — generated map of what each module does; **read before grepping** (generate/refresh with `just code-map`)
- [`.agents/CAPABILITIES.md`](.agents/CAPABILITIES.md) — generated inventory of the hooks, skills and MCP servers this project ships

## Skills (load on demand)

Before any GitHub action (create issue, branch, push, PR, merge), check
the available skills (plugin-provided in the default scaffold; `/help` lists them) and load the relevant
skill file. Skills are plain markdown (SKILL.md) — load only the one that
matches what you are about to do. Claude loads them directly; Codex and
Antigravity read them from `.agents/skills/`. Surfaces with no skills directory
(e.g. Cursor, VS Code Copilot) — and any surface in the default plugin scaffold,
where the skill files live in the plugin rather than `.agents/skills/` — instead
follow the inline quick-references in the rules below.

> Scaffolded with [project-init](https://github.com/VytCepas/project-init) on 2026-06-28.

## Key rules for agents

- **This repository is PUBLIC — a commit, an issue and a PR body all publish.** Measured with `gh repo view --json visibility`; most other trees on a contributor's machine are private, so this is the exception and the habits from the others do not transfer. Never publish, in code or in issue/PR/commit text: a client or account name, a person's or role's email address, an absolute filesystem path from a contributor's machine, or content from an agent's private configuration directory. **The exception is this package's own published metadata** — `pyproject.toml`'s `authors` field carries the maintainer's name and email deliberately, because PyPI requires it; that is *published on purpose* and must not be "fixed". The test is whether a fact is already public by intent, not whether it is on disk, and `git grep` over tracked files is how you check. **`fleet.yaml` is the load-bearing one**: it is gitignored (`.gitignore:68`) because it names every repository path on a contributor's machine, private ones included. Never `git add -f` it; `fleet.yaml.example` is the publishable shape. This rule lives here because every agent reads `AGENTS.md`, and the publication paths it protects are not Claude's alone.
- **Precedence — `context: repo`** — this repo wins on workflow, conventions and tooling. **Scoped, not total**: an ambient layer's safety and verification rules still apply wherever this file is silent.
- **Test-first for design** — for a new interface or fix.
- **Verify the premise before you plan against it** — an issue description, a stale comment, or a tool's documented behavior is a *claim*, not a fact. Check it against the code, or run the tool, before building on it. Say what you verified and how.
- **A test that cannot fail is worse than no test** — break what a guard guards, watch it fail, restore (also the helper you write to fix it). Matching *text* passes on a comment, docstring, or data; assert parsed structure or behavior, not a substring.
- **GitHub Projects** — work is tracked on the GitHub Projects board backed by GitHub Issues. Before starting non-trivial work, create or reference an issue.
- **GitHub workflow** — for any push, PR, review, or merge action, load the `github_workflow` skill. Quick ref: branch = `<type>/<KEY>-<n>-<slug>` | PR title = `type(KEY-N): desc` (no scope = no issue) | body includes `Closes #N`.
- **Commands** — the `justfile` is the canonical command surface: `just --list` shows every recipe (`setup`, `lint`, `format`, `test`, `docs`, `ci`). Prefer `just <recipe>` over raw tool invocations so every agent and CI run the same commands.
- **Lint** — `just lint` must pass before closing a task. The linter config enforces docstrings and complexity caps on project code — fix the code, don't loosen the gate.
- **Token efficiency** — tool output is re-sent on every later turn, so filter before it enters the transcript: fail-fast `just test-quick` while iterating, pipe noisy commands (`… 2>&1 | tail -n 40`), read line ranges not whole files, never re-read a file you just edited, and delegate broad sweeps to the `Explore` subagent (more tokens total — sweeps, not lookups). Caps: CLAUDE.md 200 lines, SKILL.md body 500. Full playbook: load the `token_efficiency` skill.
- **Docs** — follow the Diátaxis layout in [`docs/`](docs/) (see `docs/index.md`). Record architectural decisions with the `add_adr` skill.
- **Ownership boundaries** — each tool owns exactly one concern: uv/bun own dependencies, `just` owns commands, `.env` owns environment variables. Don't blur them (no mise tasks/env, no version pins in scripts, no commands outside the justfile).
- **No secrets in code** — never hardcode API keys, tokens, or personal data. Copy `.env.example` to `.env` (gitignored) and load it explicitly; see `.agents/docs/guides/secrets.md` for the escalation path to org secret managers.
- **No prod credentials in agent sessions** — destructive infra/DB commands are flagged by the `prod_guard` hook (a guardrail, not a guarantee). The real guarantee is credential separation: production credentials belong to review-gated CI jobs, never to a shell an agent runs in (ADR-012). Mechanics, escape hatch, per-surface behavior: `.agents/docs/guides/enforcement.md`.
- **Supply-chain package check** — package installs are registry-checked by the `package_guard` hook (missing names and typosquats flagged; fails open on network errors) — a guardrail, not a substitute for lockfile pinning. Details: `.agents/docs/guides/enforcement.md`.
- **Enforcement is agent-agnostic** — secret scanning (gitleaks) and lifecycle gating run as git hooks plus CI checks (`validate-pr`, `secret-scan`), binding every agent and human alike. Run `.agents/scripts/install_hooks.sh` once per clone to activate them.
- **Agent support tiers** — only the Claude Code path is functionally CI-tested; the real security boundary for every agent is the git/CI enforcement above. Codex: guards wired via `.codex/hooks.json` (**advisory** — some versions need a one-time enable step). Full wiring matrix: `.agents/docs/guides/enforcement.md`.


## Tests and CI

- **Iterate / before pushing** — `just test-quick` (fail-fast) while editing; `just test` when a change is done; `just ci` is the full gate CI runs.
- **Pre-push hook** — the git pre-push hook runs the local gate on every push (activate once per clone: `.agents/scripts/install_hooks.sh`). Never weaken a gate to get green.
- **Where CI runs** — `ci.yml` compute jobs run on `vars.CI_RUNS_ON` (a self-hosted runner when set, GitHub-hosted otherwise). To see where a job actually ran: `gh run view <run-id> --json jobs --jq '.jobs[] | {name, runnerName}'`. Setup: [`.agents/docs/guides/self-hosted-ci-runner.md`](.agents/docs/guides/self-hosted-ci-runner.md).
- **Not per-PR** — scheduled jobs (nightly fuzz, mutation tests) run on their cron. To run one on a branch, check the `on:` block of `ci.yml`: with a `workflow_dispatch` that has a `cadence` input, `gh workflow run ci.yml --ref <branch> -f cadence=nightly` (needs `actions: write`, else HTTP 403 — hand it to a maintainer); without one, adding it is a workflow edit (next bullet).
- **Workflow files need the `workflows` permission** — GitHub refuses a push of anything under `.github/workflows/` from a GitHub App or token without it ("refusing to allow a GitHub App to create or update workflow"). Commit, then hand the exact push command to a human with that permission; same for any PR whose branch carries such a commit.
- **Flaky locally is the machine, not the code** — if two identical runs differ (a crash, then a pass), re-run in CI before concluding anything.
- **Heavy suites belong in CI** — mutation, browser and perf suites do not run on a shared developer machine unless the task is about them.

## Claude Code specifics

This section applies only when the agent is Claude Code. Other agents get
no automatic hooks — rely on the git/CI enforcement above.

- Hooks fire automatically via the `project-init-workflow` plugin (enabled in `.agents/settings.json` under `enabledPlugins`, not a local `hooks` block): `pre_commit_gate` (lint gate on commit), `github_command_guard` (steers toward lifecycle scripts), `post_edit_lint`, `tool_output_compressor` (shrinks oversized git-diff results to a diffstat + spill-file pointer), `workflow_state_reminder`, `prod_guard`, `package_guard`. The wired scripts live under the plugin's `CLAUDE_PLUGIN_ROOT`/hooks; `.agents/hooks/` keeps only the libraries the lifecycle scripts call (`dag_workflow.py`, `prod_guard.py`, `package_guard.py`, `_py.sh`). To see or change the wiring, edit the plugin, not `settings.json`.

- Skills are auto-discovered and invocable as `/command`s.
- Review plugins: `pr-review-toolkit@claude-plugins-official` is pre-enabled in `.agents/settings.json`; also consider `/plugin install code-review@claude-plugins-official` for full PR reviews.

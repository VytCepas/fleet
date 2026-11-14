# Project: projects-orchestrator

> Cross-project orchestration layer for agentic development

Scaffolded with [project-init](https://github.com/VytCepas/project-init) on 2026-06-28.

| | |
|---|---|
| Language | python |
| Memory stack | auto |
| MCPs | none |

## Workflow

1. **Start of session** — read [`memory/MEMORY.md`](memory/MEMORY.md), then check [`.agents/docs/decisions/`](docs/decisions/) for relevant decisions.
2. **During work** — permanent decisions → [`.agents/docs/decisions/`](docs/decisions/); reusable facts → [`memory/`](memory/).

## Tools

| Type | Name | Purpose |
|---|---|---|
| Command | `/status` | git state, recent commits, open TODOs |
| Command | `/review` | code review of staged changes or a file |
| Command | `/plan <task>` | acceptance tests first, then implementation plan |
| Command | `/request-review` | mark PR ready for review, optional agent review |
| Script | `create-issue.sh <type> "desc"` | create typed issue, prints issue number |
| Script | `create-pr-without-issue.sh <type> "desc"` | branch + push + draft PR without an issue |
| Script | `setup-github.sh [branch] [--protect]` | provision board fields and review settings; `--protect` applies baseline branch protection |
| Script | `start-issue.sh <n> <type>` | branch + draft PR |
| Script | `promote-review.sh` | mark PR ready (board card moves automatically) |
| Script | `watch-pr.sh <n> [--merge]` | wait for CI; --merge squash-merges when clean |
| Script | `finish-pr.sh [n] [--review-cycle N]` | push branch, mark ready, monitor checks/review, and merge |
| Skill | `report-upstream-issue` | report a bug — routes tooling bugs upstream to project-init, keeps project bugs local |
| Skill | `token-efficiency` | token-frugal working habits for long coding/debugging sessions |
| Skill | `checkpoint` | checkpoint-and-clear session handoff (gitignored) |
| Skill | `diagram` | draw/iterate diagrams (Mermaid-first, committed source) |
| Skill | `verify-test-strength` | mutation-test a load-bearing test until survivors are killed |
| Skill | `local-ci` | Actions minutes lockout → self-hosted runner escape hatch |
| Skill | `add-hook` | add a new deterministic hook |
| Skill | `add-command` | add a new slash command |
| Skill | `audit` | full project health scan, creates issue with findings |
| Agent | `reviewer` | code review specialist |
| Agent | `researcher` | codebase explorer |

Hooks run automatically: `github_command_guard`, `workflow_state_reminder`, `pre_commit_gate`, `post_edit_lint`, `prod_guard` — supplied by the `project-init-workflow` plugin (enabled in `.agents/settings.json` under `enabledPlugins`, not a local `hooks` block); the scripts live under the plugin's `CLAUDE_PLUGIN_ROOT`/hooks, so edit the plugin to change them. Secret scanning and lifecycle gating are enforced agent-agnostically by git hooks (gitleaks pre-commit, commit-msg, pre-push — installed via `.agents/scripts/install-hooks.sh`) and mirrored in CI; the `security-guidance` plugin provides Claude-side guidance.

**Rules** (`.agents/rules/`): per-filetype conventions. Claude Code reads the copy generated in `.claude/rules/`, not `.agents/rules/`, and loads a rule when the session reads a matching file.

## Coding standards

- No comments unless the WHY is non-obvious.
- No premature abstractions — three similar lines before extracting.
- No error handling for impossible scenarios.
- No backwards-compatibility shims — delete removed code.
- Prefer editing existing files over creating new ones.
- `just lint` must pass before closing a task (`just --list` shows all recipes).


## Test-first for design

When a task shapes an interface or a fix, drive it with `/plan` — write the
acceptance tests first (the test is the contract), then implement:

1. `/plan <task>` → acceptance tests (red)
2. Commit failing tests
3. Implement until green
4. Lint and clean up

Then prove each guard can fail: break what it checks, watch it fail, then
restore — a test that passes on broken code is worse than none.

Test conventions: one assertion per test, name as `test_<unit>_<scenario>`, run with `uv run pytest`. Real DB/API instances — no mocks.


## GitHub Projects tracking

Work items are GitHub Issues; the project board is GitHub Projects (kanban with To Do / In Progress / In Review / Done columns). Board cards move automatically via `board-automation.yml` — **no manual board moves needed**.

### Lifecycle: Issue → branch → draft PR → merge

Every non-trivial task follows this exact sequence. Run the scripts; the LLM's only job is writing commit messages and fixing CI failures.

| Step | Command | What happens automatically |
|------|---------|---------------------------|
| 1. Create issue | `.agents/scripts/create-issue.sh <type> "description" --priority <priority> --area "<area>" --size <size> --acceptance "<criterion>"` | Issue created with metadata, board card → **To Do** |
| 2. Start work | `.agents/scripts/start-issue.sh <issue-n> <type>` | Branch (`<issue_type>/<project_abbr>-<issue_number>-<slug>`) + push + draft PR, board → **In Progress** |
| 3. Commit | `git add … && git commit -m "type(<KEY>-<n>): message"` | `commit-msg` hook validates format; `pre_commit_gate` auto-lints |
| 4. Push | `.agents/scripts/push-branch.sh` | CI runs; if it fails a comment is posted on the PR with `gh run view --log-failed` |
| 5. Finish PR | `.agents/scripts/finish-pr.sh <n>` | Pushes, marks ready, monitors CI/review, and merges when clean |

Steps 3–4 repeat until the work is complete. **When asked to push or finish a PR, keep going until the lifecycle is complete**: `finish-pr.sh` → fix any CI or review failures → rerun with the next review cycle until merged.

### Merge setup (one-time per repo)

Automated completion via `watch-pr.sh --merge` works after checks pass. GitHub-native auto-merge requires two repo settings enabled by an admin:

1. **GitHub Settings → General → Allow auto-merge** — tick the checkbox
2. **Branch protection rule on `main`** — require CI status checks to pass

Without GitHub-native auto-merge, `watch-pr.sh --merge` still works by merging explicitly after CI passes.

Run the scaffolded setup helper after creating the repository:

```bash
.agents/scripts/setup-github.sh --protect
```

It attempts to configure branch protection, required checks, conversation resolution, and Copilot code review. If GitHub does not expose a setting through the API or your token lacks permission, it prints the manual setup step.

### Branch model — single trunk

This project is **single-trunk**: feature PRs target `main` and merge by squash. That keeps history linear (one commit per PR, reusing the PR title) and the lifecycle simple. Environments are a *deploy-time* concern (config + deploy target), not a branching concern.

### Commit and PR format

Commit messages, PR titles, and branch names follow the quick-ref in
[AGENTS.md](../AGENTS.md) — the single source for those formats. Commit
messages use the same format as PR titles.
Types: `feat` · `fix` · `chore` · `docs` · `test`


For minor work without an issue, use `.agents/scripts/create-pr-without-issue.sh <type> "description"`.
It creates or reuses a typed `nojira` branch, pushes through `push-branch.sh`,
and opens a draft PR titled `type: description` (no scope = no linked issue).

### Rules

- **No direct commits to `main`** — all changes through PRs. The `pre-push` hook enforces this locally.
- Install hooks once after cloning:
  ```bash
  .agents/scripts/install-hooks.sh
  ```
- One issue → one branch → one PR.
- Draft PRs created immediately when work starts — not when it's done.
- PRs must pass CI (tests + lint) before merging.

### Handling CI failures

When CI fails, GitHub posts a comment on the PR. Read it, fix the code, push:
```bash
gh run view --log-failed   # full failure output
# fix the code
git add … && git commit -m "fix(<KEY>-<n>): Fix failing tests"
.agents/scripts/push-branch.sh
.agents/scripts/watch-pr.sh <n> --merge   # wait and merge when clean
```

### Code review

`finish-pr.sh` and `watch-pr.sh --merge` wait for GitHub's review decision and print review feedback when changes are requested. Resolve actionable comments, post a review response, push with `.agents/scripts/push-branch.sh`, then rerun `watch-pr.sh` with the next `--review-cycle`.

`/request-review` may also invoke the local `reviewer` agent for an extra pre-merge pass; that agent review is optional, but the GitHub PR review gate is part of the normal lifecycle.

### Quick reference

```bash
.agents/scripts/create-issue.sh <type> "description" --priority high --area docs --size S --acceptance "Done when ..."  # new issue → prints issue number
.agents/scripts/start-issue.sh <n> <type>             # branch + draft PR
.agents/scripts/create-pr-without-issue.sh fix "description" # no-issue branch + draft PR
.agents/scripts/setup-github.sh --protect            # one-time governance setup + branch protection
.agents/scripts/promote-review.sh                     # mark PR ready for review
.agents/scripts/watch-pr.sh <n> --merge             # wait for CI, merge when clean
.agents/scripts/finish-pr.sh <n>                      # push, ready, monitor, merge
gh run view --log-failed                              # see what CI failed
gh issue list                                         # open issues
gh pr list                                            # open PRs
```

## Conventions

- Hooks and scripts prefer bash/python. LLM calls only for generative steps.
- `AGENTS.md` is the canonical root instruction file (the standard most agents read natively). `CLAUDE.md` redirects to it.
- Everything else lives under `.agents/`.

## Compact Instructions

Compaction is a Claude Code mechanism; the canonical preserve-list lives in
[CLAUDE.md](../CLAUDE.md) (single source — do not duplicate it here).

# GitHub Copilot Instructions — projects-orchestrator

Canonical agent rules: [AGENTS.md](../AGENTS.md) | Workflow: [.agents/project-init.md](../.agents/project-init.md)

## Issue & project tracking

- Tracking system: **GitHub Projects** (kanban board) + **GitHub Issues** (tickets)
- Create issues: load the `create-issue` skill and use `.agents/scripts/create-issue.sh`; it handles metadata gathering, priority labels, and acceptance criteria
- Board cards move automatically via `board-automation.yml` — no manual updates needed
- Use a dedicated branch for each issue; format it as `<issue_type>/<project_abbr>-<issue_number>-<slug>`, e.g. `feat/PI-42-add-auth`; no direct commits to `main`
- Issue and PR names use a project key: `<PROJECT-KEY>-<issue-number>`, e.g. `PI-42`
- PR titles must follow Conventional Commits with the issue key as scope: `type(PROJECT-123): description` where type ∈ {feat, fix, chore, docs, test}, e.g. `feat(PI-42): Add OAuth login`
- For small no-issue PRs omit the scope: `type: description`, e.g. `fix: Fix typo`
- PR body must still include the GitHub numeric reference `Closes #N` — auto-closes issue and moves board card to Done on merge (skip for no-issue PRs)
- Draft PRs are opened immediately when work starts, not when done
- When asked to push/finish a PR, load the `github-workflow` skill for the full lifecycle and review-cycle protocol.
- No direct commits to `main`
- Branch model: single-trunk — feature PRs target `main` and squash-merge. Environments are a deploy-time concern, not a branching concern.

## Key rules

- Test-first for design (a new interface or fix); then prove each guard can fail — break what it checks, watch it fail, then restore
- No secrets in code — use `os.environ` or `.env` (gitignored)
- `just lint` must pass before closing a task (`just --list` shows all recipes)
- Prefer deterministic bash/python over LLM calls for hooks and scripts
- Permanent decisions → `.agents/docs/decisions/`; reusable facts → `.agents/memory/`

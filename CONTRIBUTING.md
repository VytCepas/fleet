# Contributing to projects-orchestrator

Cross-project orchestration layer for agentic development

This project was scaffolded with [project-init](https://github.com/VytCepas/project-init): agent
instructions live in [AGENTS.md](AGENTS.md) (canonical; `CLAUDE.md`
redirects there), and the conventions below bind humans and
coding agents alike. Claude Code gets deterministic enforcement (hooks);
other agents and humans get the same rules via git hooks and CI.

## Setup

```bash
just setup
```

Install the repo git hooks once per clone — they enforce commit-message
format, secret scanning, and branch naming locally:

```bash
.agents/scripts/install-hooks.sh
```

## Commands

The justfile is the canonical command surface — `just --list` shows every
recipe. CI runs the same recipes, so if `just lint` and `just test` pass
locally, CI agrees.

## Branches, commits, and PRs

| What | Pattern | Example |
|---|---|---|
| Branch | `<type>/<KEY>-<n>-<slug>` | `feat/KEY-42-add-oauth` |
| PR title | `type(KEY-N): description` | `feat(KEY-42): add OAuth login` |
| No-issue PR | `type: description` | `chore: bump dev dependency` |
| PR body | must include `Closes #N` (skip for no-issue PRs) | |

`KEY` is the project issue key set in `.agents/config.yaml`
(`project_key`). Types: `feat` `fix` `chore` `docs` `test`. Work starts
from an issue — use `.agents/scripts/create-issue.sh` (or the
`start-task` skill in Claude Code) so metadata lands on the project board.

## Review flow

1. Open a draft PR early; push with `.agents/scripts/push-branch.sh`.
2. Mark ready when CI is green. Respond to every review comment —
   including bot reviews — either by fixing or by explaining why not.
3. Merges go through `.agents/scripts/watch-pr.sh <n> --merge` so CI
   and review gates are honored.

## Security

See [SECURITY.md](SECURITY.md) for how to report vulnerabilities —
please do not open public issues for security problems.

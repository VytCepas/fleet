# `.agents/scripts/`

Helper scripts invoked by the user, by hooks, or by agents via the Bash tool. Prefer bash or python stdlib. Document each script with a one-line header comment.

## Available scripts

- **`install-hooks.sh`** — Symlink or copy git hooks from `.github/hooks/` to `.git/hooks/`
- **`lint-context-budget.sh`** — Fail when always-loaded context files outgrow their token budgets (CLAUDE.md/AGENTS.md 200 lines, SKILL.md 500); part of `just lint`
- **`create-issue.sh`** — Create a typed GitHub issue and print its issue number
- **`start-issue.sh`** — Create an issue branch, push it, and open a draft PR
- **`create-pr-without-issue.sh`** — Create or reuse a no-issue branch, push it, and open a draft PR
- **`push-branch.sh`** — Push current branch with retry and remote-SHA verification (handles transient 5xx errors)
- **`promote-review.sh`** — Mark a draft PR ready for review
- **`watch-pr.sh`** — Poll PR checks and optionally squash-merge with `--merge`
- **`finish-pr.sh`** — Composite: push, mark ready, monitor checks/review, and merge
- **`setup-github.sh`** — One-time GitHub repository governance setup (board fields, review settings; `--protect` applies baseline branch protection)

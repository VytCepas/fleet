---
name: start-task
description: Creates a GitHub Issue, branch, and draft PR before implementation begins. Use before any non-trivial task to keep work traceable — one issue, one branch, one PR.
when_to_use: Use when the user says "start work on X", "create a ticket for Y", or "begin a new task". Do not use for trivial one-off changes that don't need tracking.
argument-hint: "[task title]"
allowed-tools: Bash(gh *) Bash(git *) Read
---

Before starting any non-trivial task, create a GitHub Issue, a dedicated branch, and a draft PR. This keeps work traceable and every PR maps to exactly one issue.

## Mandatory scripts

| Action | Script | Never use |
|--------|--------|-----------|
| Start issue + branch + draft PR | `.agents/scripts/start-issue.sh <n> <type>` | bare `git checkout -b` + bare `gh pr create` |
| Push branch | `.agents/scripts/push-branch.sh` | bare `git push` |
| **Push + promote + merge (all-in-one)** | `.agents/scripts/finish-pr.sh <n>` | `gh pr ready`, bare `gh pr merge`, `gh pr checks --watch` |

## Steps

1. **Clarify scope** — if $ARGUMENTS is empty or vague, ask the user for:
   - Task title (one line, imperative: "Add X", "Fix Y", "Refactor Z")
   - Work type: `feat` / `fix` / `chore` / `docs` / `test`

2. **Check for existing issue and PR** — run `gh issue list` and `gh pr list`. If an issue already exists, use its number. If a draft PR already exists for that issue, use it — do **not** create a second PR. Skip to step 5.

3. **Create the issue** — load the `create-issue` skill and follow it. It gathers priority, area, size, references, dependencies, and acceptance criteria before running:
   ```bash
   ISSUE_NUMBER=$(.agents/scripts/create-issue.sh <type> "<title>" --priority <priority> --area "<area>" --size <size> --acceptance "<criterion>")
   echo "Created issue #$ISSUE_NUMBER"
   ```

4. **Start work** — create the branch, push, and open a draft PR:
   ```bash
   .agents/scripts/start-issue.sh <issue-number> <type>
   ```
   This derives the branch name (`<issue_type>/<project_abbr>-<issue_number>-<slug>`) from the issue title, pushes it, and opens a draft PR with the correct `type(PROJECT-123): description` title (Conventional Commits, ADR-006) and `Closes #n` body.

5. **Proceed** — only begin implementation after the scripts have run successfully.

6. **When ready to merge** — load the `github-workflow` skill for the
   full push → promote → monitor/merge lifecycle and review-cycle protocol.

## Rules

- Every non-trivial task must have a GitHub Issue, a branch, and a draft PR — all before the first line of implementation code.
- One issue → one branch → one PR.
- `board-automation.yml` moves the board card to **In Progress** automatically when the PR is opened. No manual board move needed.

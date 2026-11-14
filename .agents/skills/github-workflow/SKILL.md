---
name: github-workflow
description: Guides the agent through the full GitHub PR lifecycle — branch naming, push, review responses, and merge. Loaded automatically before any push, PR creation, review response, or merge action.
when_to_use: Load before any push, PR creation, PR review response, merge, or release action — including when lifecycle scripts fail and a git/gh fallback is needed.
user-invocable: false
effort: high
allowed-tools: Bash(git *) Bash(gh *) Bash(.agents/scripts/*) Read
---

Load this skill before any push, PR creation, review response, or merge action.

## Quick reference

| Step | Pattern |
|------|---------|
| Branch | `<type>/<PROJECT-KEY>-<n>-<kebab-slug>` e.g. `feat/PI-42-add-oauth` |
| PR title | `type(PROJECT-123): description` e.g. `feat(PI-42): Add OAuth login` |
| No-issue PR | `type: description` (no scope) e.g. `fix: Fix typo` |
| PR body | Must include `Closes #N` (skip for no-issue PRs) |

Commit messages use the same format (Conventional Commits). Legacy `[PROJECT-123][type]` is accepted by validators during transition but must not be emitted.

Types: `feat` `fix` `chore` `docs` `test`

**Branch model:** single-trunk — feature PRs target the repo default branch (`main`) and squash-merge. Environments are a deploy-time concern (config + deploy target), not a branching concern.

## Agent safety — the production boundary

You (the agent) work on **feature/PR branches only — never push or commit to the production ref (`main`)**. On any auto-deploy platform, write access to the production branch *is* production-deploy access. This is enforced two ways:

- **Fast-feedback (in-repo):** the command guard blocks `git push main`/`master`, `gh pr merge`, and `gh api .../merge` — but a hook is editable, so treat it as a guard rail, not the boundary.
- **The real boundary (server-side), tiered by profile (ADR-013):** run `.agents/scripts/setup-github.sh --protect` once with admin rights. For the **`org`** profile it installs rulesets with an *empty bypass list* (plus GitHub Environment reviewers for services) — a **hard** boundary no agent can edit or bypass. For **`individual`/`standalone`** it installs classic branch protection that is **advisory** (`enforce_admins=false`, so an admin-capable token can override it) — useful, but not a hard production gate; move to the `org` profile (or add a second human approver / Environment reviewer) when you need one.

## Standard lifecycle

1. **Start work** — use the `start-task` skill. It runs `start-issue.sh` which creates
   the branch, pushes, and opens a draft PR.
   For minor no-issue work, use `.agents/scripts/create-pr-without-issue.sh <type> "description"`.

2. **Push during development:**
   ```bash
   .agents/scripts/push-branch.sh
   ```
   Never use bare `git push` — `push-branch.sh` retries transient GitHub errors.

3. **Finish — push, mark ready, and merge:**
   ```bash
   .agents/scripts/finish-pr.sh [pr-number]
   ```
   `finish-pr.sh` pushes, marks the draft ready, runs `watch-pr.sh --merge`,
   and handles review cycles automatically.

   Or run steps individually:
   ```bash
   .agents/scripts/push-branch.sh
   .agents/scripts/promote-review.sh [pr-number]
   .agents/scripts/watch-pr.sh <pr-number> --merge
   ```

## Review cycle protocol

`watch-pr.sh --merge` exits **1** for CI or merge failures and **2** when
`review/decision` fails while review cycles remain. Treat any non-zero exit as
unfinished work: inspect the printed failure, fix or retry, then rerun the
workflow. Do not report a PR as merged unless the script exits 0 after printing
the merged or auto-merge-enabled status.

1. Post a response for each review comment:
   ```
   gh pr comment <pr-number> --body "**Review response:**
   - [comment]: Fixing — <reason>
   - [comment]: Not applying — <reason>"
   ```
2. Fix actionable code, then push: `.agents/scripts/push-branch.sh`
3. Re-run with the next cycle number:
   ```bash
   .agents/scripts/watch-pr.sh <pr-number> --merge --review-cycle <N>
   ```
4. After 1 review-fix cycle, the script auto force-merges with `--admin`.

**Merging needs no bypass (PI-715).** `individual`/`standalone` profiles require
zero approving reviews — an approval is unsatisfiable there, since GitHub refuses
self-approval and bot reviewers only ever COMMENT. A PR merges once CI is green, a
review has landed, and every review thread is resolved. Unresolved comments open a
review cycle (exit 2) rather than being force-merged: `required_conversation_resolution`
would reject the merge anyway, and `--admin` would discard feedback nobody answered.

**Before applying any comment:** read the current file state. Check whether the
comment is stale (already fixed), contradicts conventions, or is correct. Never
blindly apply a suggestion — post reasoning even when rejecting.

## Issue titles vs PR titles

- **Issue titles**: plain description only — type is carried by the label.
- **PR titles**: use `type(PROJECT-123): description` (Conventional Commits, ADR-006); drop the scope for no-issue PRs (`type: description`). PR titles become merge commit messages in `git log`.
- **nojira**: for minor fixes without a tracking issue; no `Closes #N` needed.

---
name: audit
description: Full project health audit — scans hooks, skills, scripts, rules, CI config, docs, and GitHub workflow for inconsistencies, broken references, and common mistakes. Creates a GitHub issue with findings and optionally starts fixing them.
when_to_use: Use when the user says "audit the project", "health check", "scan for issues", "hardening pass", or "review everything".
argument-hint: "[--fix]"
allowed-tools: Bash(git *) Bash(gh *) Bash(stat *) Bash(file *) Bash(head *) Read Write Edit Glob Grep
effort: high
context: fork
agent: general-purpose
---

Run a comprehensive project health audit. Create a GitHub issue with all findings. If `$ARGUMENTS` contains `--fix`, also fix the issues and open a PR.

## Phase 1 — Inventory

Read `.agents/config.yaml` for project metadata (language, memory stack, MCPs). This determines which checks apply.

Scan these directories and build a file inventory:

| Directory | What to check |
|---|---|
| `.agents/hooks/` | All hook scripts |
| `.agents/skills/` | All SKILL.md files |
| `.agents/scripts/` | All lifecycle scripts |
| `.agents/rules/` | All rule .md files |
| `.agents/docs/` | ADRs, conventions |
| `.agents/memory/` | MEMORY.md index |
| `.github/workflows/` | CI/CD configs |
| `.github/hooks/` | Git hooks |

## Phase 2 — Checks

Run each check category. Track findings as `[PASS]`, `[WARN]`, or `[FAIL]`.

### 2.1 Hook integrity

For every file in `.agents/hooks/`:

1. **Executable bit** — `stat -c '%a' <file> 2>/dev/null || stat -f '%Lp' <file> 2>/dev/null` (GNU then BSD/macOS) must show execute permission
2. **Hook protocol** — must output JSON to stdout and `exit 0` (never `exit 1` to block). Search for:
   - `exit 1` that isn't inside a `trap` or error path before JSON parsing → `[FAIL]`
   - `>&2` used for block output instead of stdout → `[FAIL]`
   - Missing `exit 0` at end of file → `[WARN]`
3. **JSON parsing** — must use Python via the `python-resolve.sh` resolver (not jq) for portability. `grep -l 'jq ' *.sh` → `[FAIL]`
4. **Stdin reading** — must read `$CLAUDE_TOOL_USE_STDIN` or stdin JSON. Check the INPUT= line exists.
5. **Referenced in settings.json** — every hook file should appear in `.agents/settings.json`. Orphaned hooks → `[WARN]`

### 2.2 Script integrity

For every file in `.agents/scripts/`:

1. **Executable bit** — must be executable
2. **Shebang** — first line must be `#!/usr/bin/env bash` or `#!/usr/bin/env python3`
3. **set -euo pipefail** — bash scripts must have it
4. **Referenced in docs** — check `.agents/scripts/README.md` and `.agents/project-init.md` mention the script. Undocumented scripts → `[WARN]`

### 2.3 Git hooks

For every file in `.github/hooks/`:

1. **Executable bit** — must be executable
2. **Shebang** — must have one

### 2.4 Skill and command consistency

For every SKILL.md in `.agents/skills/*/`:

1. **Required frontmatter** — `name` and `description` must exist
2. **Listed in index.md** — `.agents/skills/index.md` should reference every skill. Missing → `[WARN]`
3. **Listed in project-init.md** — `.agents/project-init.md` Tools table should list it. Missing → `[WARN]`

### 2.5 Settings.json coherence

1. **Every hook file referenced** has a corresponding file in `.agents/hooks/`. Dead references → `[FAIL]`
2. **Hook files referenced** in settings.json must exist on disk with the correct snake_case names

### 2.6 Documentation cross-references

1. **project-init.md** — every script, skill, command, hook, and agent listed in the Tools table must exist on disk
2. **scripts/README.md** — every script in the directory should be documented
3. **rules/hooks.md** (if present) — every hook listed must exist; every existing hook must be listed
4. **Commit format** — verify that project-init.md, copilot-instructions.md, conventions.md, and config.yaml all agree on the commit/PR format (`type(KEY-N):`, ADR-006)

### 2.7 CI workflow validation

1. **Language match** — CI commands must match the project language from config.yaml:
   - Python: `uv run ruff`, `uv run pytest`
   - Node: `bun run lint`, `bun test`
   - Go: `golangci-lint run`, `go test`
   - None: no lint/test steps expected
2. **PR validation workflow** — if `validate-pr.yml` exists, check its title regex matches the format in project-init.md

### 2.8 Memory and vault

1. **MEMORY.md exists** and is a valid index (links to files that exist)
2. **Broken links** — any `[text](file.md)` in MEMORY.md where file.md doesn't exist → `[FAIL]`

## Phase 3 — Report

Compile findings into a structured report:

```markdown
## Project Audit Report

**Project:** <name from config.yaml>
**Date:** <today>
**Language:** <language>

### Summary
- X checks passed
- Y warnings
- Z failures

### Failures
1. <file>: <what's wrong> — <how to fix>

### Warnings
1. <file>: <what's wrong>

### All Checks
<full checklist with [PASS]/[WARN]/[FAIL] markers>
```

## Phase 4 — Create issue

Create a GitHub issue using the project's lifecycle script:

```bash
.agents/scripts/create-issue.sh chore "Project audit findings" \
  --priority medium \
  --area "tooling" \
  --size M \
  --acceptance "All [FAIL] items resolved, all [WARN] items reviewed"
```

Paste the full report as a comment on the issue.

## Phase 5 — Fix (only if --fix)

If `$ARGUMENTS` contains `--fix`:

1. Start work on the issue: `.agents/scripts/start-issue.sh <n> fix`
2. Fix all `[FAIL]` items. Review `[WARN]` items and fix where appropriate.
3. Run lint and tests after fixes.
4. Push and finish: `.agents/scripts/finish-pr.sh <n>`

If `--fix` is not specified, stop after Phase 4 and report the issue number.

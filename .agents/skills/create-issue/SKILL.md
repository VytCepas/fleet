---
name: create-issue
description: Creates a GitHub Issue with typed labels and structured planning metadata via create-issue.sh. Sub-skill invoked by start-task — do not call directly; use /start-task instead.
when_to_use: Invoked by the start-task skill whenever a GitHub Issue must be created; never called directly by users.
user-invocable: false
allowed-tools: Bash(gh *) Bash(.agents/scripts/*) Read
---

Use this skill whenever creating a GitHub Issue.

## Metadata to gather

Before creating the issue, determine:

- type: `feat`, `fix`, `chore`, `docs`, or `test`
- title: short imperative description
- priority: `high`, `medium`, or `low`
- area: existing repo area label or plain body metadata
- size: `XS`, `S`, `M`, `L`, or `XL`
- scale: `task` (default — one focused PR) or `epic` (a large initiative tracked as the parent of small child tasks)
- references: related issues, PRs, ADRs, docs, designs, logs, or external links
- dependencies: blocked-by, parent, or follow-up relationships
- acceptance criteria: concrete checklist items

If type, title, priority, area, size, or acceptance criteria are not clear from context, ask the user before proceeding (scale defaults to `task` — only set `epic` for a large parent initiative).

## Rules

- **Keep tickets small.** An `epic` may be large, but every child ticket must be `--scale task` and sized `S`/`M` (split anything that would be `L`/`XL`). Small tickets mean small PRs and bounded context — where AI-assisted implementation works best.
- Use `.agents/scripts/create-issue.sh`; do not call `gh issue create` directly unless the script cannot satisfy the case.
- Do not invent labels. The script may create priority, size, and scale labels, but area labels are repository-specific.
- Store relationships that GitHub does not support portably in markdown sections.
- The script writes Definition of Ready/Done defaults so issues created from this skill satisfy the scaffolded issue-validation workflow.
- Check for duplicate issues before creating a new one:
  ```bash
  gh issue list --state open --search "<keywords>"
  ```

## Create

Run:

```bash
.agents/scripts/create-issue.sh <type> "<title>" \
  --priority <high|medium|low> \
  --area "<area>" \
  --size <XS|S|M|L|XL> \
  --scale <task|epic> \
  --reference "<reference>" \
  --dependency "<dependency>" \
  --acceptance "<criterion>"
```

Repeat `--reference`, `--dependency`, and `--acceptance` as needed.

## Report

After creation, report the issue number and URL:

```bash
gh issue view <number> --json url -q .url
```

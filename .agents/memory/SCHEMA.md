# Memory Schema

Authoritative governance for `.claude/memory/` files. `lint-memory.sh` enforces these rules.

## Required frontmatter

Every memory file (except MEMORY.md, SCHEMA.md, README.md) must have:

```yaml
---
name: <short title>
description: <one-line summary used for relevance ranking>
type: user | feedback | project | reference
---
```

## Types

| Type | Purpose | Body structure |
|---|---|---|
| `user` | About the human — role, preferences, expertise | Free-form |
| `feedback` | Rules/corrections the user has given | **Why:** + **How to apply:** |
| `project` | Current-state facts — goals, deadlines, decisions | **Why:** + **How to apply:** |
| `reference` | Pointers to external systems — URLs, dashboards | Free-form |

## File naming

Convention (not enforced by lint): `<type>_<slug>.md` — lowercase, hyphenated slug.

Examples: `user_role.md`, `feedback_testing.md`, `project_deadline.md`, `reference_api-docs.md`

## Index

Every memory file must appear in `MEMORY.md` as a one-line bullet:

```markdown
- [Title](filename.md) — short description
```

Keep lines under ~150 characters. `lint-memory.sh` checks for orphaned files and stale index entries.

## What NOT to store

- Code patterns or architecture (derivable from the repo)
- Git history (`git log`)
- Ephemeral task state (use TODOs)
- Anything already in `CLAUDE.md` or `project-init.md`
- Large documents (put those in `vault/`)

## Relationship to vault

`memory/` holds small structured facts for fast agent recall. `vault/` holds richer human-authored documentation (Obsidian). When a vault note distills into a reusable fact, create a memory file and link back to the vault note for context.

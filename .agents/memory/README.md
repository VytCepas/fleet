# `.agents/memory/`

Small, grep-able, human-readable memory files. Intended for facts an agent should reuse across sessions.

See [`SCHEMA.md`](SCHEMA.md) for the authoritative governance rules (types, frontmatter, naming, lint enforcement).

## Convention

Every memory file has YAML frontmatter:

```markdown
---
name: <short title>
description: <one-line summary used for relevance ranking>
type: user | feedback | project | reference
---

<body>
```

### Types

| Type | When to use |
|---|---|
| `user` | About the human (role, preferences, expertise). |
| `feedback` | Rules/corrections the user has given. Include **Why** and **How to apply**. |
| `project` | Current-state facts about the project (deadlines, decisions, stakeholders). Include **Why** and **How to apply**. |
| `reference` | Pointers to external systems (GitHub projects, dashboards, channels). |

### What NOT to save

- Code patterns/architecture (derivable from the repo)
- Git history (use `git log`)
- Ephemeral task state (use TODOs)
- Anything already in `CLAUDE.md` or `project-init.md`

## Index

`MEMORY.md` in this directory is the index — one line per memory file. Keep it tight.

## Why this split exists

- `memory/` = small structured facts, agent-curated, fast to grep.
- `vault/` = human-authored documentation (Obsidian). Larger, richer.

## Graphify (if present)

If this project was scaffolded with the `obsidian-graphify` preset,
`.agents/rules/graphify.md` documents the code knowledge graph: query
`graphify-out/graph.json` before grepping, rebuilt per commit. One-time
setup: `.agents/scripts/setup-graphify.sh`.

---
kind: reference
owner: @VytCepas
status: unreviewed
---

# Descriptor contract v1

The orchestrator is a **reader** of the contract that
[project-init](https://github.com/VytCepas/project-init) scaffolds into every
child project. This page pins the exact surfaces the engine consumes today, so
the coupling between the two tools is a **named, versioned boundary** rather
than implicit knowledge spread across the code.

Anything not listed here is *not* read by the orchestrator and carries no
compatibility promise. Everything listed here should stay stable within
contract version `1`; a breaking change is a new contract version, surfaced in
the `Contract` column of `projects-orchestrator status`.

> Scope: the orchestrator only ever *reads* these files. It never writes the
> contract, and it degrades (never raises) when any surface is missing or
> malformed — see [ADR-003](../../.claude/docs/adr/adr-003-fleet-engine.md).

> **Layout (PI-627).** A current project-init scaffold keeps its canonical tree
> under **`.agents/`**, not `.claude/`. The paths below are written `.claude/…`
> for historical continuity, but the orchestrator resolves the scaffold root via
> `descriptor.resolve_config` — `.agents/` first, `.claude/` as a legacy
> fallback for pre-PI-627 projects — and reads every surface (config,
> `CAPABILITIES.md`, memory, observability) from that root. Read each `.claude/`
> below as "the scaffold config root".

## 1. `config.yaml` — the descriptor

Parsed by `descriptor.py`. A directory is considered a project-init project iff
this file exists and is readable.

| Key | Type | Consumed as | Missing → |
|---|---|---|---|
| `project.name` | string | project name | directory name |
| `project.project_init_contract_version` | int | `Contract` column; `0`/absent means "predates the contract" | `0` (`none`) |
| `project.project_init_version` | string (`MAJOR.MINOR.PATCH`) | `Scaffold` column + `Latest` staleness compare | `unknown` (not comparable) |
| `language` | string | descriptor `language` | `unknown` |
| `delivery` | string (`library`/`service`/`prototype`) | descriptor `delivery` | `unknown` |
| `memory.tier` | int (`0`–`3`) | memory tier; selects the retrieval surface. **Derived from `memory.stack`** when the stack is on the ladder; a tier that disagrees is warned about (so `doctor` fails `config`), never obeyed (#257) | `0` |
| `memory.stack` | string | declared backend (`auto`/`obsidian-only`/…); the source of truth for the tier | `unknown`; `none` when the whole `memory:` block is absent at contract ≥ 1 (a declined project renders no block, #257) |
| `memory.memory_path` | string (repo-relative) | memory directory location (the anchor) | `.claude/memory` |
| `memory.vault_path` | string (repo-relative) | Obsidian vault; **read only at tier ≥ 1** | `None` |
| `memory.graph_path` | string (repo-relative) | graphify graph; **read only at tier ≥ 2** | `None` |
| `memory.rag_endpoint` | string (URL/addr) | RAG query endpoint; **read only at tier ≥ 3** | empty |
| `tooling.<task>_command` | string (shell) | the gate run for `<task>` (`lint`, `test`, `run`, …) | task is `skip` |

Notes:

- **Tooling.** Only keys ending in `_command` with a non-empty string value are
  read; the `_command` suffix is stripped to form the task name. An undeclared
  gate is never guessed — it is skipped.
- **Tier-gated memory surfaces (project-init ADR-024; project-init ADR-025 §4).** `vault_path`, `graph_path`,
  and `rag_endpoint` are *higher-tier retrieval surfaces*: each is read only at
  or above the tier that introduces it (1, 2, 3 respectively). The anchors
  (`tier`, `memory_path`, `MEMORY.md`) never move between tiers — higher tiers
  only **add** surfaces — so a tier-0 reader stays correct against a tier-3
  child, and a stray higher-tier value on a lower-tier config is ignored —
  with a warning, so a surface declared below its gate is distinguishable from
  one never declared (#236). `vault_path`/`graph_path` escaping the project
  root, or not resolvable at all, are dropped with a warning at any tier,
  exactly like `memory_path`. Retrieval degrades by tier
  (`memory.retrieval_mode`): RAG → graph → grep.
- **Version format.** `project_init_version` is compared by splitting on `.`
  into integer components (`0.5.2` → `(0, 5, 2)`). Any non-integer component
  (e.g. `1.2.0-rc1`) makes the version *not comparable* — the `Latest` cell
  renders `-` rather than guessing an order. Staleness is judged relative to
  the **newest comparable version in the fleet**, so it is fully offline.

## 2. `scaffold.manifest` — drift detection

Read by `drift.py` from the **same `config.yaml`**, under `scaffold.manifest`:

```yaml
scaffold:
  manifest:
    <repo-relative-path>: <sha-256-hex>
    ...
```

The engine hashes each listed file in the working tree and compares. A project
with no `scaffold.manifest` reports `no-manifest` (not an error). This is what
lets drift detection fall out of the existing contract for free.

## 3. Git hooks — enforcement health

Read by `drift.py`. Hooks shipped in `.github/hooks/` are compared against
`.git/hooks/` in the child's clone:

- `ok` — every shipped hook is installed and matches its tracked source
- `stale` — every shipped hook is installed, but at least one differs from
  `.github/hooks/`: a merged hook change that was never re-installed
- `partial` / `missing` — some / none installed
- `-` — the project ships no hooks

Enforcement that exists in `.github/hooks/` but was never installed into
`.git/hooks/` never actually runs — so install state is a first-class signal.

## 4. `.claude/memory/*.md` — fleet memory

Read and searched by `memory.py`. Each fact file carries YAML frontmatter:

```markdown
---
name: <title>
description: <one-line summary>
type: user | feedback | project | reference
---

<markdown body>
```

- Index/schema files (`MEMORY.md`, `SCHEMA.md`, `README.md`) are treated as the
  index, not as facts.
- `name` falls back to the file stem, `description` to empty, `type` to
  `unknown` when frontmatter is absent — malformed files degrade to untyped
  entries rather than failing the read.

## 5. `.claude/CAPABILITIES.md` — capability inventory

Read by `capabilities.py` (the `capabilities` command). project-init generates
this surface-independent inventory (project-init ADR-017 §6) of the skills, hooks, and MCP
servers the scaffold gave the agent, as markdown section tables
(`## Skills (N)`, `## Hooks`, `## MCP servers (N)`). The orchestrator parses
those tables and inverts them across the fleet — *which projects expose which
skill/MCP* (project-init ADR-025 §3). A missing or malformed file degrades to an empty
inventory, never an error.

## 6. `project-init --json <target>` — the registration seam

Read by `adapters/project_init.py` (`parse_scaffold_result`) and the `register`
command. project-init emits this JSON "for a root orchestrator driving
project-init" (#510): the freshly-scaffolded `target`, its `contract_version`,
`config` path, `memory` tier/stack, `files_created`, and `conflicts`. `register`
adds `target` to the orchestrator's own fleet file so the next command governs
the new project — no manual edit, no second config read. Numbers emitted as
strings (`"1"`) are tolerated; a document with no `target` is rejected. This is
the one place the orchestrator *writes* — to its **own** registry, never to a
child tree (ADR-003).

## Machine source of truth

project-init ships a shared `descriptor.schema.json` (VytCepas/project-init#603),
packaged as a consumable via #786 (`project_init.schema.load_descriptor_schema`),
as the machine-checkable definition of the surfaces above. Validating the golden
fixtures under `fixtures/deploy/scaffolder/` against it directly is tracked in
PO #90. Meanwhile the producer→consumer contract test (`tests/check/fleet/test_contract.py`)
is the tripwire: it parses **real** project-init scaffolds — a legacy `.claude/`
v1 and a current `.agents/` v2 — through every reader above, so an upstream shape
change or relocation fails CI. See [contract v2](descriptor-contract-v2.md).

## Compatibility policy

- Fields in this document are the v1 surface. The orchestrator pins to
  `project_init_contract_version: 1`.
- project-init may **add** fields freely; the orchestrator ignores unknown keys.
- **Removing or repurposing** any field above is a breaking change and requires
  a new contract version. Consumers must never reach into project-init
  *implementation* details (specific script internals, undocumented file
  layouts) — only the surfaces listed here.

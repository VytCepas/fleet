---
name: diagram
description: Draw and iterate on diagrams with the user — system architecture, code structure, data models, flows, or idea sketches — as version-controlled Mermaid/DOT/Excalidraw source with live previews
when_to_use: Use when the user says "draw", "diagram", "sketch", "visualize the architecture", "schema", "flowchart", "sequence diagram", "ERD", "mindmap", or wants to see how components/ideas relate.
user-invocable: true
---

# Diagram — draw it together, keep the source

Diagrams here are **source files first** (Mermaid by default): diffable,
regenerable, and reviewable like code. Renders are previews; the committed
source is the artifact.

> **Not for data charts.** This skill draws *structural* diagrams — components,
> flows, states, schemas, ideas. If the request is a chart/plot/dashboard over
> *data* (numbers → bars, lines, points), it does not apply: reach for a
> plotting library suited to the stack (matplotlib/plotly, Vega-Lite, Recharts,
> or a spreadsheet) rather than forcing the data into Mermaid.

## 1. Pick the notation by diagram type

| The user wants to see… | Use |
|---|---|
| Components/flow/dependencies | Mermaid `flowchart` (`LR` for pipelines, `TB` for hierarchies); add `layout: elk` frontmatter when >15 nodes |
| Interactions over time | Mermaid `sequenceDiagram` |
| Lifecycle / states | Mermaid `stateDiagram-v2` |
| Data model / tables | Mermaid `erDiagram` |
| Modules / classes | Mermaid `classDiagram` |
| Brainstorm / idea map | Mermaid `mindmap` |
| Timeline / plan | Mermaid `gantt` |

**Escalations (only when Mermaid genuinely fails the job):**
- Dense graph that Mermaid tangles even with `layout: elk` → Graphviz **DOT
  source**. Render only if `dot` is installed (`command -v dot`); otherwise
  ship the source and say plainly that no local renderer is available.
- Free-form idea sketch with spatial meaning → **Excalidraw JSON** (open and
  edit at excalidraw.com). No local render — say so; don't pretend.

## 2. Ground before drawing

- **Code/architecture mode:** derive nodes and edges from reality — read
  `code-map.md` (if present), imports, and directory structure. Never invent
  a component; if a box isn't backed by a file/module/service you can name,
  it doesn't go on the diagram.
- **Label file-backed nodes with their repo-relative path** — a "scaffold
  engine" box reads `scaffold engine\n(src/project_init/scaffold.py)`. The path
  is a *verifiable* fact, so a later check can flag a diagram that still points
  at a moved or deleted file. Keep volatile specifics — thresholds, version
  pins, line numbers — *out* of labels: point at their source, don't restate
  them (map-not-territory; they rot silently).
- **Idea mode:** one interview round first — what are the entities, what
  relations matter, what question should the diagram answer?

**Worked grounding pass** (a repo's module layout):

1. Enumerate what actually exists — `ls src/` (or the stack's source root) and
   read `code-map.md` if present. Don't guess from memory.
2. Keep ≤25 real nodes; group or drop the rest into an overview diagram.
3. Every file-backed node carries its path, so each box is checkable:

   ```mermaid
   flowchart LR
     cli["wizard CLI\n(src/project_init/__main__.py)"] --> engine["scaffold engine\n(src/project_init/scaffold.py)"]
     engine --> tmpl["templates/ — the product"]
   ```

4. A box you cannot back with a real path or service name does not go on the
   diagram.

## 3. The iteration loop

1. Each diagram gets its own folder, named after the task/topic (kebab-case
   slug): `docs/diagrams/<slug>/` — or `.agents/vault/design/<slug>/` when
   the project has an Obsidian vault (its existing home for "diagrams, spec
   drafts"). Everything about that diagram lives inside: the source, the
   rendered picture, and any other related assets (notes, alternate views,
   exported data). Write the source to `docs/diagrams/<slug>/<slug>.mmd` —
   or `.agents/vault/design/<slug>/<slug>.mmd` in the vault case.
2. **Always render a picture file for human viewing, every time the source
   changes** — not only on request. From the repo root:
   `bunx @mermaid-js/mermaid-cli -i docs/diagrams/<slug>/<slug>.mmd -o docs/diagrams/<slug>/<slug>.svg`
   (swap in the vault folder when that's the diagram's home). This is
   unconditional: the folder should never be left with only a `.mmd` and no
   picture.
3. Preview: on Claude Code, also send the `.mmd` file inline (the side panel
   renders Mermaid natively) so iteration doesn't require re-rendering.
4. Ask **one** targeted question per round — "right boxes?", "right
   arrows?", "right grouping?" — not "any feedback?".
5. Apply feedback as **small edits, never wholesale regeneration**. Keep
   node IDs stable across rounds so the source diff shows exactly what
   changed. Re-render the picture after every edit round — it must stay in
   sync with the source, never stale.

## 4. Quality rules

- **≤ ~25 nodes per view.** Past that, split: one overview diagram plus
  drill-down diagrams per area. A mega-graph answers no question.
- `subgraph` blocks for layers and boundaries (UI / core / infra;
  trusted / untrusted).
- Label every edge whose relation isn't obvious from the endpoints.
- Title the diagram; add a legend when shapes/styles carry meaning.
- Never encode meaning in color alone (accessibility; renders vary).

## 5. Finalize

- Commit the whole `<slug>/` folder: source **and** rendered picture
  together. The source is the artifact of record for diffing/regeneration;
  the picture is what a human opens without tooling — both ship, always.
  **Commit the render, don't gitignore it:** GitHub renders ```` ```mermaid ``` ````
  fences but not `.mmd` files as images, so the checked-in picture is the only
  way a human sees the diagram on the forge without local tooling. It's
  collapsed in `.gitattributes` — `docs/diagrams/**/*.svg` as
  `linguist-generated`, and a vault render under `.agents/vault/**` already as
  `linguist-vendored` — so it doesn't inflate diffs or language stats, and the
  always-re-render rule (§3) keeps it from going stale.
- Embedding:
  - GitHub/GitLab render ```` ```mermaid ``` ```` fences in markdown natively —
    inline the source in docs/README where useful.
  - mkdocs needs one-time config; offer to add it:

    ```yaml
    markdown_extensions:
      - pymdownx.superfences:
          custom_fences:
            - name: mermaid
              class: mermaid
              format: !!python/name:pymdownx.superfences.fence_code_format
    ```

- A diagram that drifted from the code is worse than none: when the
  underlying structure changes, update the source in the same PR — that is
  why the source, not the picture, is the committed artifact.

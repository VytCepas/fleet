---
kind: explanation
owner: @VytCepas
status: unreviewed
---

# Interfaces: one view-model, four surfaces

The orchestrator presents the fleet through four surfaces — the CLI status
table, the controller REPL, the Textual TUI, and the web dashboard (`serve`)
plus its static sibling (`snapshot --html`). They look different but they are
all **projections of the same view-model**, so what one shows can never
disagree with another.

## The shared layer

Everything the surfaces render flows from
[`fleet.py`](../../src/projects_orchestrator/fleet.py):

- **`COLUMNS`** — the ordered column set. It is defined once; every surface
  iterates it.
- **`snapshot_row` / `fleet_rows`** — pure functions that turn a
  `ProjectSnapshot` into a `column → cell text` dict. This is the single place
  that decides *what text a cell holds*. Adding a column means adding it to
  `COLUMNS` and giving it a value in `snapshot_row` — nothing else changes,
  because every surface reads the dict by column name.
- **`cell_status`** — a pure, presentation-free classifier mapping a cell's
  text to `good` / `bad` / `warn` / neutral. This is the single source of
  truth for status colouring.

## How each surface projects it

| Surface | Rows from | Status via |
|---|---|---|
| CLI table (`render_table`) | `fleet_rows` | (plain text) |
| `snapshot --html` (`html.py`) | `fleet_rows` | `cell_status` → CSS class |
| TUI (`tui.py`) | `fleet_rows` | `cell_status` → terminal colour |
| `serve` web dashboard (`server.py`) | `fleet_rows` | `cell_status`, emitted per-cell in the JSON payload → CSS class |

The controller REPL's `status` verb renders the same `render_table` output as
the CLI.

## Why it matters

Before this consolidation the good/bad/warn vocabulary was copied into three
places (the HTML frozensets, the TUI's style map, and the dashboard's
client-side JavaScript) and had already drifted — the TUI never coloured a
running project's `up …` cell because its copy of the vocabulary predated that
value. Routing every surface through `cell_status` removed the duplication and
the drift: the web dashboard now receives the status from the server rather
than re-deriving it in the browser, and the TUI colours `up …` like everything
else. A change to the status vocabulary — or to what any cell contains — is now
a one-place edit that every surface inherits.

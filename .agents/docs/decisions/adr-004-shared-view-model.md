# ADR-004: One view-model behind every interface surface

- Status: accepted
- Date: 2026-07-05

## Context and Problem Statement

The orchestrator presents the fleet through four surfaces — the CLI status
table, the controller REPL, the Textual TUI, and the web (`snapshot --html`
and the live `serve` dashboard). How do we keep them from disagreeing? The
rows were already shared via `fleet_rows`, but the good/bad/warn **status
vocabulary** had been copied into three places (the HTML frozensets, the TUI
style map, and the dashboard's client-side JavaScript) and had already
drifted: the TUI never coloured a running project's `up …` cell because its
copy predated that value. Adding a column or a status word meant editing
several files, and forgetting one produced a silent inconsistency.

## Considered Options

- Leave each surface to classify cells independently (status quo).
- Share only the row text (`fleet_rows`) and accept per-surface styling copies.
- Add one pure, presentation-free classifier that every surface maps to its
  own styling.

## Decision Outcome

Chosen option: "one pure classifier", `fleet.cell_status(value) ->
good|bad|warn|neutral`. The text table, HTML (`cell_status` → CSS class), TUI
(`cell_status` → terminal colour), and the `serve` dashboard (status emitted
per cell in the JSON payload, not re-derived in the browser) all consume it.
Combined with `COLUMNS` + `snapshot_row` already owning the column set and cell
text, the view-model is now the single place that decides *what a cell says and
what status it carries*; a surface only decides how to paint it.

### Consequences

- Good: a change to the status vocabulary — or to any cell's content — is a
  one-place edit every surface inherits; the `up …` drift is fixed and cannot
  recur.
- Good: the web dashboard carries no status vocabulary of its own (removed the
  duplicated JS sets); the server is the source of truth.
- Bad: adding a genuinely surface-specific rendering (e.g. a sparkline column,
  #64) still requires threading its data through `snapshot_row`, which every
  surface then renders — the coupling is intentional but must be kept cheap
  (read shared inputs once per fleet render, not per project).

See `docs/explanation/interfaces.md` for the narrative walkthrough.

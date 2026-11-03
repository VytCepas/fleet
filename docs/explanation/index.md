---
kind: explanation
owner: @VytCepas
status: unreviewed
---

# Explanation

Understanding-oriented discussion: why the system is shaped the way it is,
trade-offs considered, context that doesn't fit a recipe or a reference
table. Architectural decisions belong in `.agents/docs/adr/` — link them
from here when they need narrative.

- [Overview: what the orchestrator does and how it relates to project-init](overview.md)
  — the whole system on one page: capabilities, the read-only project-init
  contract boundary, and the architecture. A rendered visual version ships at
  [`docs/overview.html`](../overview.html).
- [Interfaces: one view-model, four surfaces](interfaces.md) — how the CLI,
  controller, TUI, and web dashboard all project the same fleet view-model.

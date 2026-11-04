---
kind: reference
owner: @VytCepas
status: unreviewed
---

# projects-orchestrator

Cross-project orchestration layer for agentic development

This documentation follows the [Diátaxis](https://diataxis.fr/) structure —
put each page where its *purpose* fits, not where its topic fits:

| Section | Purpose | Reader is… |
|---|---|---|
| [Tutorials](tutorials/index.md) | Learning by doing | new, following along |
| [How-to guides](how-to/index.md) | Solving a specific problem | working, goal in hand |
| [Reference](reference/index.md) | Looking up facts (API, config) | working, needs precision |
| [Explanation](explanation/index.md) | Understanding why | studying, wants context |

Architectural decisions live in `.agents/docs/adr/` in the repository (not
part of this site) — use the `add_adr` skill to record one.

Decision records with a box-wide id (`D-<n>` here, `fleet/D-<n>` from any other repo) are the
files in [`docs/decisions/`](decisions/0001-record-decisions.md); the series starts at D-1.
The ADRs above predate it and keep their numbers.

> Tip (public repos): [DeepWiki](https://deepwiki.com/) generates free,
> always-current architecture docs from the repository — link
> `deepwiki.com/<owner>/<repo>` from your README.

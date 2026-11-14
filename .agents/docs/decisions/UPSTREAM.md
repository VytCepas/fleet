# ADRs cited from project-init

Some decisions this repo depends on were made in
[project-init](https://github.com/VytCepas/project-init/tree/main/docs/adr), the
scaffolder that produces the descriptor this orchestrator reads. They are cited
here as `project-init ADR-NNN`. A bare `ADR-NNN` always means a file in this
directory.

Every `project-init ADR-NNN` cited in this repo must have a row below, and every
row must be cited somewhere. `tests/check/fleet/test_decision_record_citations.py` enforces both, so a
mistyped number fails instead of pointing at nothing.

| Cited as | File in project-init `docs/adr/` | Cited here for |
|---|---|---|
| project-init ADR-012 | `adr-012-prod-safety-guard.md` | §2: credential separation is the boundary. Agent sessions hold no production credentials, and review-gated CI jobs do. |
| project-init ADR-017 | `adr-017-per-surface-config-generator.md` | §6: the surface-independent capabilities inventory (`CAPABILITIES.md`) |
| project-init ADR-024 | `adr-024-memory-tier-model.md` | the memory tier ladder |
| project-init ADR-025 | `adr-025-agentic-os-root-layer.md` | §1: one-way producer→consumer dependency. §3: the capability inventory the root layer aggregates. §4: the descriptor contract and degrade-by-tier retrieval |
| project-init ADR-013 | `adr-013-distribution-governance-model.md` | the estate is not only public `github.com`: a remote's host travels with the repository `gh` is pointed at (spike #254 — Enterprise Cloud, GHE.com, GHES) |

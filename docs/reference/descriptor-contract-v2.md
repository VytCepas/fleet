---
kind: reference
owner: @VytCepas
status: unreviewed
---

# Descriptor contract v2

Status: **live** — project-init emits `project.project_init_contract_version: 2`
with the blocks below, and the orchestrator parses them. This page pins the v2
surfaces beyond [contract v1](descriptor-contract-v1.md); the contract itself is
owned upstream (project-init) and this is a *reader's* record of what the
orchestrator consumes.

> Layout note (PI-627): a current project-init scaffold keeps its canonical tree
> under `.agents/`, not `.claude/`. The descriptor is `.agents/config.yaml`; the
> `.claude/` projection deliberately excludes it. The orchestrator resolves
> `.agents/` first and falls back to `.claude/` for pre-PI-627 projects
> (`descriptor.resolve_config`).

## Invariants (unchanged from v1)

- **Additive only.** Every v1 surface keeps its meaning; a v1 reader that
  ignores the new blocks keeps working. The orchestrator likewise ignores the
  v2 blocks on configs declaring contract version `< 2` — exactly as a v1
  reader would.
- **Machine-generated.** The scaffold renders these fields; humans edit them
  through project-init, not by hand.
- **Not hash-covered.** The descriptor is NOT in `scaffold.manifest`, and
  `drift` does not see an edit to it. The manifest lives inside the descriptor,
  so the descriptor cannot record its own hash; project-init's
  `write_scaffold_record` skips it by name, and it is user-owned besides
  (`project_key` and the like). Measured 2026-09-19: none of six real manifests
  lists it. What partly checks the descriptor instead is this reader. SOME
  malformed or contradictory values become a warning that `doctor`'s `config`
  check FAILs on: an unreadable contract version or tier, a tier that
  contradicts its stack, an escaping path. Others are coerced to defaults
  silently. A non-mapping `deploy:` reads as `target: none`, and a non-list
  `hooks.expected` reads as empty. An edit that stays well-formed is not
  detected by anything (#212, #220).
- **Read-only.** The orchestrator never writes any of this (ADR-003).

## New surfaces

### `tooling.run_command`

Already representable in v1 (any `<task>_command` is read); v2 makes it a
*named, expected* field so the `Runnable` column and the controller `run`
verb rest on contract rather than convention.

### `deploy:` — runtime identity for `delivery: service` projects

```yaml
deploy:
  target: fly          # none | cloud-run | fly | k8s | …
  app: my-service      # app/service name at the target
  region: fra          # when the platform needs one
  health_url: https://my-service.example/healthz
  workflow: deploy.yml # the child's workflow_dispatch deploy pipeline
```

Consumed by `adapters/cloud.py` (`cloud-status` command + `Cloud` column for
reads; the `deploy` command for actions):

| Key | Type | Consumed as | Missing → |
|---|---|---|---|
| `deploy.target` | string | which platform CLI probes run (`flyctl`, `gcloud`) | `none` (no probe, zero cost) |
| `deploy.app` | string | app name interpolated into the probe command | empty |
| `deploy.region` | string | region interpolated into the probe command | empty |
| `deploy.health_url` | string (http/https) | bounded stdlib GET → `healthy`/`unhealthy`/`unknown` | no health probe |
| `deploy.workflow` | string | the child workflow the `deploy` command dispatches for cloud actions | `deploy.yml` (convention) |

`deploy: none` — or omitting the block — stays valid and free: the adapter
short-circuits with no subprocess and no network call.

**Reads are read-only; actions are dispatch-only.** The status probes above
never mutate. The `deploy` command (`deploy`/`rollback`/`restart`) does *not*
run `flyctl`/`gcloud` locally either — it dispatches the child's own
`workflow_dispatch` pipeline (`deploy.workflow`) with an `action` input, so
production credentials stay in the child's review-gated CI and never enter the
orchestrator. This is the same dispatch pattern as `upgrade-plan --apply`, and
it is dry-run by default (only `--apply` fires). See
[ADR-005](../../.claude/docs/adr/adr-005-cloud-control-plane.md).

### `observability.path` — where guard/usage logs live

```yaml
observability:
  path: .claude/observability
```

Consumed by `observability.py` (`events` command). Today the location is an
undocumented convention; v2 names it so fleet ingestion doesn't guess. When
undeclared (or at v1) the orchestrator falls back to
`.claude/observability/`. A declared `path` that escapes the project root
(`../…` or absolute) is ignored with a warning — the orchestrator only ever
reads inside the project it governs. The log itself stays `usage.jsonl`, one
JSON object per line; the reader tolerates `ts`/`timestamp` and
`action`/`decision`/`event` aliases (project-init's guards log the outcome
under `event`), passes through an optional `session` id so a run's events group
across projects, skips (and counts) malformed lines, and counts events whose
timestamp is present but unparseable.

**Timestamps.** Event and `--since` instants may be ISO-8601
(`2026-07-04T10:00:00Z`) or raw epoch-seconds; a naive ISO stamp is read as
UTC. This is the pinned timestamp contract for the log.

### `hooks.expected` — the git hooks the scaffold ships

```yaml
hooks:
  expected: [pre-commit, commit-msg, pre-push]
```

Consumed by `drift.hook_health` (the `Hooks` column and `doctor`). With the
list declared, hook health checks the *contract* — are these exact hooks
installed in `.git/hooks/`? — instead of globbing `.github/hooks/`, which
conflates "what the scaffold ships" with "what happens to be in the tree".
Undeclared (or v1) keeps the globbing fallback.

## Orchestrator implementation state

`descriptor.py` parses all of the above behind `contract_version >= 2`
(`DeployConfig`, `observability_path`, `hooks_expected`), covered by
`tests/status/fleet/test_descriptor.py` and — against a **real** `.agents/`-layout v2 scaffold
— by `tests/check/fleet/test_contract.py`. v0/v1 children are unaffected: the fields stay at
their empty defaults and every consumer falls back to the v1 behavior.

**Read-surface hardening (orchestrator-side, done).** `memory_path` and
`observability.path` are clamped under the project root (an escaping value is
rejected with a warning, never read); `doctor` warns on a `contract_version`
newer than the orchestrator understands (`CONTRACT_VERSION_MAX`) instead of
claiming full conformance; the observability timestamp format is pinned above.

**Emitted upstream.** project-init emits `project_init_contract_version: 2` with
the `deploy` / `observability.path` / `hooks.expected` blocks and `run_command`
(verified by the golden `config-v2.yaml` fixture and `tests/check/fleet/test_contract.py`).
The orchestrator still `shlex.quote`s `deploy.app`/`region` defensively; a
stricter `^[A-Za-z0-9._-]+$` constraint at scaffold time would be a nice-to-have
upstream hardening, not a blocker.

**Machine source of truth.** project-init ships the shared
`descriptor.schema.json` + `usage-event.schema.json` (VytCepas/project-init#603,
packaged as a consumable via #786 — `project_init.schema.load_descriptor_schema`).
Validating the golden fixtures directly against it is tracked in PO #90; until
that lands, the producer→consumer contract test (`tests/check/fleet/test_contract.py`) —
which parses the real `.agents/`-layout v2 fixture through every reader — is the
tripwire.

**Registration seam (consumed).** project-init's `--json` output
(#510) is no longer a tested-but-dead seam: `adapters/project_init.parse_scaffold_result`
+ the `register` command read it to register a freshly-scaffolded project
into the fleet without a second config read. See
[contract v1 §6](descriptor-contract-v1.md).

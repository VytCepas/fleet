# ADR-005: Cloud actions are a control plane, not a data plane

- Status: accepted
- Date: 2026-07-05

## Context and Problem Statement

The orchestrator reads deploy/runtime state today (`cloud-status`,
`adapters/cloud.py`) but cannot *act* on it — there is no way to deploy,
roll back, or restart a `delivery: service` project from the one control
surface. Adding cloud actions collides head-on with project-init ADR-012: production
credentials belong to review-gated CI jobs, never to a shell an agent runs
in. How do we give the fleet real cloud control without ever putting cloud
credentials in the orchestrator's process?

## Considered Options

- **Direct execution.** The orchestrator runs `flyctl deploy` /
  `gcloud run deploy` / `kubectl rollout restart` itself (e.g. via a declared
  `tooling.deploy_command`). Simple, but the orchestrator's shell then needs
  production cloud credentials — exactly what project-init ADR-012 forbids.
- **Dispatch.** The orchestrator triggers the child repo's own
  `workflow_dispatch` pipeline (`gh workflow run deploy.yml -f action=…`); the
  mutation runs in CI, where the credentials and environment protections live.
  This is already how `upgrade-plan --apply` works
  (`adapters/project_init.trigger_upgrade`).
- **No cloud actions.** Stay observe-only. Rejected: the fleet's whole value
  is being the single control point, and deploy/rollback/restart are the most
  requested actions.

## Decision Outcome

Chosen option: **dispatch**. Cloud actions are added as
`adapters/cloud.trigger_deploy(descriptor, action)` which dispatches the
child's own deploy workflow (`deploy.workflow`, defaulting to `deploy.yml`)
with an `action` input of `deploy` | `rollback` | `restart`. The orchestrator
holds no cloud credentials and runs no platform mutation itself — it *decides
and dispatches*, the child's CI *executes with the creds*. This keeps the
orchestrator a **control plane, not a data plane**, and reuses the exact
pattern ADR-003 / project-init ADR-012 already blessed for scaffold upgrades.

Two guardrails make it safe to expose from an interactive/agent surface:

1. **Dry-run by default.** `trigger_deploy` returns a `planned` result and
   makes no subprocess call unless the caller passes `apply=True`. The
   `deploy` CLI verb mirrors `upgrade-plan`: it prints what it *would*
   dispatch and does nothing until `--apply` is given.
2. **The REPL/TUI cockpit is plan-only.** The controller `deploy` verb never
   dispatches — it always reports the plan and points at `deploy --apply`.
   The mutation requires the explicit, non-interactive CLI flag.

Both guardrails are asserted on *behaviour* (no subprocess is launched), not on
the returned status — a test that only checks for `planned` passes just as
happily when a dry run is firing real dispatches.

**What these guardrails do NOT do — be precise, or the safety story is a lie.**
The plan-only cockpit stops a *typo* and a careless line in a REPL. It does not
contain an agent: an agent with shell access simply runs
`orchestrator deploy api --action rollback --apply`. Nothing here prevents that,
and nothing here could — the CLI is the sanctioned mutation path, and an agent
holds the same CLI a human does.

The real boundary is therefore **not** in this repo. It is the child's own
workflow: `gh workflow run` against an *unprotected* `workflow_dispatch` workflow
deploys to production immediately, with no review, agent or not. "Dispatch to a
review-gated pipeline" is a property of the child (a GitHub Environment with
required reviewers, or branch protection on the deploy ref) — a convention this
system relies on and, today, does not verify. `doctor`'s `deploy-workflow` check
confirms the workflow *exists*; it does not confirm anyone reviews it. Treat an
unprotected child deploy workflow as production access granted to every holder of
the orchestrator CLI.

### Consequences

- Good: real fleet-wide cloud control from one repo, with **zero** cloud
  credentials in the orchestrator — project-init ADR-012 credential separation is preserved
  by construction, not by discipline.
- Good: consistent with `upgrade-plan --apply`; `trigger_deploy` degrades to
  `failed` offline (always *with a reason* in `detail`) and to `skipped` for
  non-service projects, exactly like the rest of the never-raise engine (ADR-003).
  A GitLab child dispatches via `glab`, as `trigger_upgrade` already does.
- Good: every action is auditable — it is a CI workflow run, subject to whatever
  branch/environment protections the child has configured, not an opaque local
  command. Note "whatever": see the guardrail caveat above; this system does not
  verify that such protections exist.
- Bad: the child must own a `workflow_dispatch`-enabled deploy workflow that
  accepts an `action` input; a project without one reports `no-workflow` — up
  front, on the dry run, and via `doctor`, rather than as a mystery `failed` at
  `--apply` time. This is the intended coupling — the child declares what is runnable, the
  orchestrator only triggers it.
- Bad: dispatch is fire-and-forget — `trigger_deploy` confirms the workflow was
  *queued*, not that the deploy *succeeded*. Deploy outcome is observed
  afterward through the existing read path (`cloud-status`, the `Cloud`
  column). ~~Closing that loop (poll-until-settled) is deferred.~~
  **`deploy --wait` closes it (#152)** — opt-in, because fire-and-forget is the
  right *default* (decide and dispatch, let the review-gated path execute). With
  `--wait` the CLI reads a pre-dispatch run-id watermark, follows the run the
  dispatch creates, and reports `succeeded`/`failed`/`timed-out`/`unconfirmed`,
  exiting nonzero on anything short of a confirmed success. It never claims an
  outcome it did not observe — an unappearing run is `unconfirmed`, an
  unreachable `gh` is `unknown`, never a silent success.

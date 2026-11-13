---
name: local-ci
description: Diagnose a GitHub Actions billing/minutes lockout and move CI to a self-hosted runner — the escape hatch when a private repo runs out of free minutes
when_to_use: Use when CI jobs fail to start, the PR merge gate stalls with checks that never register, or the user mentions "out of Actions minutes", "billing", "spending limit", or "CI won't run".
user-invocable: true
---

# Local CI — when GitHub Actions minutes run out

On a **private** repo that exhausts its Actions minutes (or hits a
spending-limit/payment problem), every job fails at start and the required
checks never report — the merge gate stalls. Verification itself never
stops (see step 2); only the remote checks do.

## 1. Diagnose — confirm it's the billing lockout

```bash
gh run list --limit 5 --json conclusion,displayTitle
```

The lockout signature is `startup_failure` with the annotation *"The job was
not started because recent account payments have failed or your spending
limit needs to be increased."* Check remaining minutes:

```bash
# Enhanced billing platform (current; filter for Actions usage):
gh api "/users/{username}/settings/billing/usage" --jq '[.usageItems[] | select(.product == "actions")]'
gh api "/organizations/{org}/settings/billing/usage" --jq '[.usageItems[] | select(.product == "actions")]'
# Accounts not yet on the enhanced platform: the legacy endpoint still answers:
gh api /users/{username}/settings/billing/actions
```

If runs are failing for any *other* reason, this skill does not apply — fix
the actual failure.

## 2. Immediate unblock — the gate already runs locally

`just ci` runs the **same recipes CI runs** (the justfile is the single
command surface), and the pre-push git hook already enforces it on every
push. Nothing about correctness is blocked — keep working; only the merge
gate needs the fix below.

## 3. Durable fix — self-hosted runner (zero billed minutes)

Follow `.agents/docs/guides/self-hosted-ci-runner.md`: register a runner
(**`--ephemeral` recommended**), verify it shows online under Settings →
Actions → Runners, then:

```bash
just ci-local-on    # sets the CI_RUNS_ON repo variable → compute jobs run on your runner
```

Checks keep reporting to GitHub; branch protection and the merge gate stay
intact. Revert any time with `just ci-local-off`.

**Do not set the variable before the runner is online** — jobs queue forever,
which is quieter and worse than the billing error.

**Known-dead checks still block merges (PI-837).** The secret/PAT-bearing
workflows stay GitHub-hosted (see safety rails) and keep failing as zero-step
`startup_failure` runs while the lockout lasts — and the lifecycle PR merge
monitor (where installed) blocks on any failing check. Name them in `.agents/config.yaml`, e.g.
`monitor_ignore_checks: ["board-sync"]` (per-run:
`PI_MONITOR_IGNORE_CHECKS=board-sync`): they are then reported as
informational instead of blocking. Remove the entry once billing recovers.
Note check runs attach to *commits*, not PRs — a stale failure inherited from
a sibling branch at the same head clears with an empty head-refresh commit
(details: `.agents/docs/guides/self-hosted-ci-runner.md`).

## 4. Honest alternatives

- Make the repo **public** — Actions becomes free.
- Raise the spending limit (Settings → Billing).
- Wait for the monthly reset (usage resets on your billing date).

## Safety rails (non-negotiable)

- The secret/PAT-bearing workflows are deliberately pinned to GitHub-hosted
  runners — never point them at `CI_RUNS_ON`.
- Never bypass required checks by posting statuses from local runs —
  self-attestation defeats the gate; the runner path keeps real checks.
- Self-hosted runners are for private repos with trusted collaborators only;
  never for public-fork PRs.

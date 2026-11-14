# ADR-007: Agent runs are isolated, PR-bounded, and never reach the data plane

- Status: accepted
- Date: 2026-07-14
- Supersedes: **ADR-006 §2** ("the fix runs on a dedicated branch, never in
  place" — heal's clean-worktree requirement and its `checkout -B` in the
  operator's own clone). See §3 below for why that mechanism could not survive
  contact with a fleet.

## Context and Problem Statement

The orchestrator can *observe* the fleet (`status`, `ci`, `audit`, `doctor`,
`drift`, `history`, the digest) and *control* parts of it (`supervisor` starts
processes, `deploy` dispatches to a child's CI, `upgrade` runs project-init).
What it cannot do is **put an agent to work**. Every remediation the fleet
surfaces — a red CI, a missing scaffold, an unresolved drift — still ends with a
human opening a terminal, `cd`-ing into the project, and typing `claude`.

For a single operator maintaining a large estate, that is the bottleneck. The
fleet already knows *what is wrong and where*; the missing verb is one that acts
on it. Concretely: rolling project-init out across an unscaffolded estate is a
dozen near-identical mechanical jobs, and doing them by hand is exactly the work
an agent should do.

But an agent with write access to every repo in the estate is a genuinely
dangerous object, and the orchestrator already sits one function call away from
production (`deploy`, ADR-005). How do we let agents act across the fleet
without handing a non-deterministic process the authority to land code or touch
production?

## Considered Options

- **Interactive multiplexer.** The orchestrator opens and manages N interactive
  agent sessions, and the operator tabs between them. Rejected: this is fourteen
  terminal tabs with extra steps. It does not scale past the number of sessions
  a human can hold in their head, which is the exact limit we are trying to lift.
- **Agent-as-a-tool-caller.** Expose the orchestrator's verbs to an LLM (the
  CopilotKit / `useFrontendTool` shape) and let it decide what to run. Rejected:
  it puts a non-deterministic process directly on top of `deploy`, and it forces
  a Node runtime into a project whose entire runtime dependency is `pyyaml`.
- **Dispatch, isolate, and bound the output.** The orchestrator launches a
  *headless* agent in an isolated git worktree with an injected briefing, and the
  agent's only permitted output is a draft PR. Nothing merges; nothing deploys.
  This is the same shape ADR-005 chose for cloud actions — *decide and dispatch,
  let a review-gated path execute* — applied to code changes instead of deploys.

## Decision Outcome

Chosen option: **dispatch, isolate, and bound the output.**

A **run** is the unit: one agent, one task, one project. It gets an id, a git
worktree, a captured log, and a state record under `$XDG_STATE_HOME` — reusing
the machinery `supervisor.py` already has for long-running processes, since a
headless agent *is* a long-running process.

Five properties define the design.

### 1. A run's lifecycle outlives its process

The supervisor's model is binary: the pid is alive or it is not. An agent run has
a state that model cannot express — *"finished, opened PR #14, awaiting review."*
That is neither running nor dead.

So the run record survives process exit, and its terminal states are
`pr-opened`, `failed`, `needs-human`, and `abandoned` — **not** an exit code. A
design that equates "process gone" with "run over" will show an empty fleet table
while fourteen PRs sit unreviewed.

### 2. `needs-human` is a first-class terminal state

A headless agent that hits an ambiguity has no one to ask. It must not guess, and
we must not turn the orchestrator into an interactive multiplexer to let it ask.

Instead the agent stops, records why, leaves its worktree intact, and surfaces as
`needs-human` in the fleet table. `work <project> --attach` then drops the
operator into an **interactive** session in that same worktree with the same
context already loaded. Headless by default, human on demand — and it is precisely
this escape hatch that makes headless-by-default safe to run unattended.

### 3. The write boundary is enforced orchestrator-side, not by the child

Every run happens in a fresh `git worktree` (not the operator's working clone),
on a branch, and its only sanctioned output is a **draft PR**. It never pushes to
`main` and never merges. The child project's own review-gated CI decides whether
the work lands — the same credential-separation principle as project-init ADR-012, applied to
code instead of secrets.

**This must be enforced here, in the run harness.** A project-init'd repo has a
`pre-push` hook that blocks pushes to main, and it is tempting to lean on it. But
the first campaign we want — the project-init rollout — **targets exactly the
projects that do not have that hook yet**. The child's guard is absent precisely
where the blast radius is highest. A guard that is missing whenever it matters is
not a guard.

**A worktree, not a checkout — this supersedes ADR-006 §2.** ADR-006 chose to run
the fix "on a dedicated branch, never in place": require a clean worktree,
`git checkout -B` in the project's own clone, and restore the operator's branch on
every exit path. That is the right *intent* and the wrong *mechanism*, and it does
not survive contact with a fleet:

- **It commandeers the operator's clone.** A fleet-wide heal branch-switches every
  project out from under whoever is working in them.
- **"Restores on every exit path" is not true, and cannot be made true.** It rests
  on a `finally`, and a SIGKILL, an OOM, or a host that sleeps badly skips it —
  stranding a clone on a `heal/` branch with an agent's uncommitted edits in the
  tree. An invariant that a `kill -9` can break is not an invariant.
- **It serialises everything.** Every property above — a detached run, two runs at
  once, a run whose record outlives its process — is impossible while a run owns
  the clone's HEAD. ADR-006 §2 is not merely unsafe; it is the single thing
  blocking the rest of this design.

A `git worktree` gets the same isolation with none of that: it shares the object
store (so it is cheap), it is a *second* checkout rather than a hijacked one, and
the operator's clone is only ever read. The clean-worktree requirement disappears
along with the reason for it — a run no longer cares what state your working copy
is in, so a project can be healed while you are mid-edit in it.

### 4. The data plane is unreachable from an agent run

The orchestrator can dispatch deploys (ADR-005). An agent running inside the
orchestrator therefore sits one call away from production. It does not get that
call: the agent runner exposes a strict allow-list of verbs, and `deploy` is not
on it, whatever the agent asks for. Deploys stay human-initiated.

Blocking the *verb* is not sufficient, and saying otherwise would be the kind of
half-true safety story ADR-005 warns against. An agent that cannot call `deploy`
can still find `gcloud`, `gsutil`, or `flyctl` on the PATH and reach production
underneath us. So the run's **environment is scrubbed of cloud credentials**: the
agent cannot mutate a Cloud Run service, a bucket, a database, or a secret,
because it never holds anything that would let it. This is project-init ADR-012 turned on the
agent — *it cannot leak, or wreck, what it does not have.*

The asymmetry is what justifies the severity. A bad code change is a PR you
reject. A bad `gsutil rm` is gone. **Code is reversible; state is not**, and an
agent's whole world is therefore a git worktree — never the data plane.

This also settles what happens to a GCP-hosted project: nothing. The repo is the
unit and GCP is merely where it lands. The agent edits code and opens a PR; the
child's CI holds the cloud credentials and does the deploying. Whether a project
runs on Cloud Run, writes to a bucket, or talks to a Cloud SQL is invisible to
the agent, and must stay that way.

This is a narrower claim than ADR-005's, and deliberately so. ADR-005 is explicit
that its plan-only cockpit "does not contain an agent: an agent with shell access
simply runs `orchestrator deploy api --action rollback --apply`." That remains
true and this ADR does not repeal it. What we control is the *orchestrator's own*
agent runner — the runs it launches, and what it puts in front of them. An agent
the operator starts by hand, in their own shell, holds the same CLI a human does.
The boundary here is on the runs this system spawns, not on agents in general.

### 5. A campaign terminates when its selector empties

A campaign is a task, a selector, and a policy:

```yaml
name: project-init-rollout
select: scaffold=none
task: |
  Apply project-init to this repo, then make `just ci` pass.
policy: { max_concurrent: 3, timeout: 30m, output: draft-pr }
```

The **selector is the progress bar**. As PRs merge, projects gain a scaffold and
fall out of `scaffold=none` on their own. Completion is *derived*, never tracked,
and re-running a campaign is naturally idempotent — it picks up whatever is still
outstanding and ignores the rest. This mirrors the freshness check's discipline:
compute the answer from the world, do not maintain a parallel record of it that
can drift.

A campaign also **canaries by default**: it runs one project, and does nothing
further until `--apply`. Forty agents launched at once is forty sessions of real
money spent on a task prompt nobody has validated yet. The first PR is the proof
the prompt was right; buying that proof for one run instead of forty is not a
safety feature bolted on, it is just the correct way to work.

### The briefing: inject what the agent cannot cheaply discover

The injected context is the entire value-add over the operator `cd`-ing in and
typing `claude` themselves — and the temptation is to stuff it. A bloated prompt
is worse than none.

The agent reads `AGENTS.md` natively (project-init scaffolds it); do not duplicate
it. It can read the code; do not paste it. What it cannot cheaply get is **why it
was summoned**: the actual CI failure output, the specific `doctor` findings, the
`drift` diff, the descriptor's declared gates. That, plus the output contract
(*your only output is a draft PR; the gate is `just ci`; you do not merge*), is
the briefing. Nothing else.

## Consequences

- Good: the fleet's existing knowledge becomes *actionable*. A red CI or a missing
  scaffold stops being a row in a table and becomes a draft PR you review.
- Good: the operator's review load scales with *outcomes*, not with projects. The
  project-init rollout becomes twelve PRs to read instead of twelve migrations to
  perform.
- Good: reuses the never-raise engine wholesale (ADR-003). A run that cannot push
  (no remote, no `gh` auth) does not crash the fleet — it degrades to `failed`
  *with a reason* and leaves the worktree on disk with the work in it. Nothing is
  silently discarded.
- Good: no descriptor contract change. Runs, campaigns, and policy are all
  consumer-side state; the child repos are unchanged. project-init ADR-025's one-way,
  pull-only relationship with project-init holds.
- Bad: failed runs accumulate worktrees on disk. This is deliberate — a dead
  agent's worktree is the only forensic record of what it was thinking, and
  deleting it on failure would destroy the evidence at the exact moment it is
  needed. Retention is bounded by an expiry, not by discarding on sight.
- ~~Bad: cost is bounded but not metered. Concurrency caps, a wall-clock timeout,
  and canary-first put a leash on spend; none of them report what a run actually
  cost. Per-run cost accounting is deferred.~~
  **Closed by #146.** A run now records what it cost, parsed from the result
  object the CLI was already writing to its log, and a campaign totals its spend
  — so a canary reports not just whether the work landed but what the fan-out
  would cost at that rate. The consequence this leaves behind is narrower: a run
  that is killed or times out never reports its spend, so it is *unmetered*. That
  is surfaced as a known-unknown (an em-dash, and a count on every total) rather
  than summed as zero — a timeout is the priciest way to fail, and totalling it
  as free would under-report spend at exactly the moment it spiked.
- Bad: dispatch is fire-and-forget in the same sense ADR-005 is. A run confirms a
  PR was *opened*, not that the change was *correct*. Correctness is established
  by the child's CI and by the human reading the PR — which is the point, but it
  means a campaign's green completion is not evidence its work was good.
- Bad, and load-bearing: **anything without a repo is invisible to this system.**
  A Cloud Function pasted into the console, an unowned bucket, a service deployed
  from someone's laptop — none of them have a descriptor, so none of them are in
  the fleet, and no agent can be pointed at them.

  This is treated as a *signal*, not a defect to engineer around. If a thing
  cannot be `work`ed on, that is because it has no repo, no CI, no gates and no
  review — there is nothing about it that could be changed safely. The remedy is
  to give it a repo, not to widen the agent's reach into GCP.

  What is needed is therefore *discovery*, not orchestration: a read-only cloud
  inventory (`gcloud asset search-all-resources`) diffed against the fleet, so the
  unmanaged estate is at least *enumerable* — an orphan gets a repo, gets
  project-init, becomes a fleet member, and only then becomes something an agent
  may touch. Read-only, holding no write credentials, degrading to `unknown` when
  unauthenticated; an unauthed scan reporting "zero orphans" would be the worst
  possible lie this system could tell.

  *Since accepted:* this is **built** — `orphans` (#126, `orphans.py` +
  `adapters/gcp.py`), exactly as specified above, down to refusing an unscoped
  scan rather than declaring an estate clean it never fully looked at.

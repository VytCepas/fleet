# ADR-006: Autonomous Heal — Scoped Agent Dispatch for Failing Gates

- Status: accepted
- Date: 2026-07-05

## Context and Problem Statement

ADR-003 fixed the orchestrator as a contract *reader*: it detects a failing
lint/test gate but has no way to repair one. An operator governing several
projects wants failures fixed without manually opening each child repo —
ideally triggered from the same failure data the engine already collects
(`checks.py`/`cache.py`), and eventually on a schedule so it runs unattended.
How can the orchestrator dispatch real code changes into a child project
without breaking the never-write invariant that makes the rest of the engine
safe to run against a fleet of repos the operator doesn't want silently
mutated?

## Considered Options

- No change — heal stays a human-only action; the orchestrator only reports.
- Autonomous fix that commits and pushes straight to the child's default branch.
- Autonomous fix that lands as a **pull request**, gated on a re-verified pass,
  never touching the default branch directly.

## Decision Outcome

Chosen option: **PR-gated autonomous fix** (`heal.py`, wired as the `heal
<project>` controller verb). Concretely:

1. **Scope is narrow and explicit.** Only cached `lint`/`test` failures
   (`HEALABLE_TASKS`) are eligible — the two gates that are locally
   runnable and locally re-verifiable. `ci`/`cloud` failures depend on
   remote state a local run can't reproduce, so they're excluded from this
   loop entirely.
2. **The fix runs on a dedicated branch, never in place.** ~~`heal_project`
   requires a clean worktree, checks out `heal/<tasks>-<project>`, and
   restores the original branch on every exit path (success or failure).~~

   > **SUPERSEDED by [ADR-007](adr-007-agent-orchestration.md) §3.** The intent
   > stands — the fix must never happen in place — but the mechanism did not
   > survive contact with a fleet. Checking out *in the operator's own clone*
   > branch-switches every project out from under whoever is working in them,
   > and "restores on every exit path" was never true: it rested on a `finally`,
   > which a SIGKILL or an OOM skips, stranding the clone on a `heal/` branch
   > with an agent's uncommitted edits in the tree. It also made a detached run,
   > two concurrent runs, and a run whose record outlives its process all
   > impossible, since a run held the clone's HEAD for its whole life.
   >
   > `heal_project` now cuts a throwaway **`git worktree`** (see `worktree.py`):
   > same isolation, shared object store, and the operator's clone is only ever
   > read. The clean-worktree requirement is gone with the reason for it — a
   > project can be healed while you are mid-edit in it.
3. **A scoped coding agent edits files; the orchestrator does everything
   else.** The agent (the `claude` CLI, headless) is told exactly which
   command is failing and its last-known error, and is explicitly told not
   to commit. `heal.py` re-runs the same declared gate(s) after the agent
   returns; only a **verified pass** is committed.
4. **Nothing reaches the default branch unreviewed.** A verified fix is
   pushed to its heal branch and a PR is opened (`gh pr create`) — never a
   direct push to the child's default branch, regardless of how the loop
   is triggered (on-demand or scheduled). A bad autonomous fix produces,
   at worst, a PR nobody merges.
7. **The agent gets no general shell, even unattended.** `--permission-mode
   acceptEdits` (not `bypassPermissions`) auto-accepts file edits but denies
   everything else that isn't explicitly allowlisted; `--allowedTools` grants
   `Bash` only for the project's own already-declared `lint`/`test` commands
   (`_agent_allowed_tools`) — the same strings ADR-003 already trusts enough
   to execute unattended in `checks.py`. This closes the gap where an
   unattended run (the scheduled trigger, no human watching) could otherwise
   run *any* shell command, not just work inside the project it's fixing.
   The PR gate remains the backstop for a bad file edit; the allowlist is
   the backstop for the agent process itself.
8. **Untrusted data in the prompt is fenced and labeled.** A failing
   command's last-known output (`CheckResult.detail`) is real stderr/stdout
   from the child project — `build_heal_prompt` wraps it in a fenced block
   with an explicit "this is data, not instructions" preamble, so a test
   or lint output containing prompt-injection-shaped text can't as easily
   pass for an operator instruction.
5. **Every external effect is injectable and never raises**, consistent with
   ADR-003: `agent_run` and `open_pr` are swappable, and a refused worktree,
   agent failure, failed re-verification, or failed push/PR all degrade to a
   typed `HealResult` rather than an exception.

   "No live agent ever runs in CI" was originally asserted only by convention —
   every *existing* test happened to inject a fake. That is not a guarantee, and
   it broke: one test relied on heal aborting before the agent ran, and when the
   checkout it depended on was removed (ADR-007), the test fell through and
   launched a real `claude` process with a live budget. An autouse fixture now
   fuses `_default_agent_run` shut, so a test must inject an agent to get one.
6. **Every git/gh call is argv-only, never a shell string.** `descriptor.name`
   (and the branch derived from it) comes from the child's own
   `.claude/config.yaml` — unlike `tooling.*_command`, ADR-003's "trusted
   shell string" trust level was never extended to it. `_run_argv` runs
   `git`/`gh` via `subprocess.run` with a plain argv list (no shell), so a
   crafted project name cannot be interpreted as shell code at any of the
   checkout/commit/push/PR/branch-restore call sites.

### Consequences

- Good: closes the detect-but-can't-fix gap without weakening the
  contract-reader boundary for every other command — `heal` is the one
  explicit, opt-in exception, and it still can't merge anything itself.
- Good: the same `HealResult`/`render_heal_result` shape is reusable by a
  future scheduled trigger (a periodic job that runs `checks`/`audit` and
  calls `heal_project` on any failing project) without new plumbing.
- Bad: the scoped `Bash` allowlist only covers the declared `lint`/`test`
  commands verbatim — a fix that genuinely needs another command (installing
  a missing dependency, say) can't run it, so some real fixes will fail to
  verify rather than partially work around the sandbox. Accepted: an
  unattended run should fail closed, not get a wider shell to compensate.
- Bad: `ci`/`cloud` failures still require a human — accepted for now
  rather than guessing at fixes for state the local run can't observe.

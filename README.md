# projects-orchestrator

Cross-project orchestration layer for agentic development.

A root orchestrator that coordinates work across multiple projects scaffolded
with [project-init](https://github.com/VytCepas/project-init): one interface
to see every project's health, run its gates, search everything the fleet
remembers — and **put an agent to work on what it finds**.

## Install

Not on PyPI yet. Install from a checkout with `just install --apply` (without
the flag it is a dry run), reinstall after every pull, and `just install --check`
reports a build that no longer matches the checkout:
[Install and upgrade the orchestrator itself](docs/how-to/install-and-upgrade.md).
Changes per release are in [CHANGELOG.md](CHANGELOG.md).

## Usage

### See the fleet

```sh
projects-orchestrator projects            # list discovered projects
projects-orchestrator status              # fleet table: health/branch/lint/tests/memory
projects-orchestrator checks [project]    # run each project's own lint/test gates (--jobs N, --changed-only)
projects-orchestrator memory <query>      # search every project's memory files
projects-orchestrator capabilities [project]  # who exposes which skill/MCP/hook (--kind to invert the fleet)
projects-orchestrator drift [project]     # scaffold drift vs the recorded manifest
projects-orchestrator doctor [project]    # diagnose contract-v1 conformance
projects-orchestrator audit [project]     # one governance report (--markdown; --digest for a delta vs last run)
projects-orchestrator hardening [project] # setup-readiness checklist with next actions
projects-orchestrator ci [project]        # latest CI conclusion + open PR/MR count (gh, or glab for gitlab.com hosts)
projects-orchestrator cloud-status [project]  # deploy/runtime status (contract-v2 deploy block)
projects-orchestrator events [project]    # guard/usage events from observability logs (--since ISO)
projects-orchestrator history <project>   # per-task check-history trend (sparkline) + pass/fail transitions
projects-orchestrator notify [project]    # threshold alerts (CI red/drift/hooks/cloud) — --webhook to push
projects-orchestrator orphans --scope projects/<id>  # live GCP resources no repo accounts for (read-only)
```

### Act on it

```sh
projects-orchestrator work <project> "<task>"  # launch a tracked agent run — lands a DRAFT PR, never merges
projects-orchestrator work --list              # every run: state, cost, and where to look
projects-orchestrator work <project> --attach  # take over a needs-human run, interactively
projects-orchestrator campaign <file>          # declarative fleet campaign: canary first, --apply to fan out
projects-orchestrator heal <project>           # fix a red lint/test gate via a scoped agent — lands a DRAFT PR
projects-orchestrator heal --all --limit 3     # heal the whole fleet, unattended-safe (caps spend); schedulers ship for Linux/systemd only — see docs/how-to/scheduled-heal.md
projects-orchestrator deploy <project>         # dispatch the child's deploy workflow (--action deploy|rollback|restart; --apply)
projects-orchestrator start <project>          # launch the project's run_command (detached, logged)
projects-orchestrator stop <project>           # terminate the supervised process
projects-orchestrator logs <project>           # tail the captured run output (-n lines)
projects-orchestrator upgrade-plan             # scaffold version vs upstream (--apply to trigger upgrades)
projects-orchestrator register <json>          # register a project from `project-init --json <target>`
```

### Surfaces

```sh
projects-orchestrator snapshot --json     # full machine-readable fleet state
projects-orchestrator snapshot --html -o fleet.html  # self-contained HTML dashboard
projects-orchestrator serve               # live auto-refreshing dashboard (--host, --port)
projects-orchestrator serve --enable-actions  # + re-check/heal buttons (loopback only, CSRF-token gated)
projects-orchestrator controller          # deterministic command REPL (also: `heal <project>`)
projects-orchestrator tui                 # terminal UI (needs the tui extra)
```

Every data command accepts `--json` for external monitors, and exit codes
are meaningful (`checks` exits 1 when any gate fails, `drift` when any
project diverged from its scaffold, `doctor` when any project fails
contract-v1 conformance, `audit` when anything needs attention, `upgrade-plan`
when any project is behind upstream project-init), and `--json` exits exactly
as the text mode does. A degraded cell
(`unknown`, `-`, `?`) never stops a command. `--verbose` (or
`PROJECTS_ORCHESTRATOR_VERBOSE=1`) logs to stderr every error a degraded path
swallowed: an unreadable file, a failed probe, unparseable output. A cell that
degraded with no error behind it (an empty answer, a gate never probed) is not
logged. `upgrade-plan` names the reason for every `unknown` row itself. An
unexpected internal error exits 70 with one line, and `--verbose` adds the
traceback. `audit` is
the one-shot governance report: it composes `doctor`'s conformance findings
with scaffold-drift divergence, a memory-schema lint, and check freshness
(`--markdown` renders a digest for a scheduled run). The status table tracks per project:
health · branch · sync · scaffold version · scaffold freshness (vs the
newest in the fleet) · descriptor-contract version · drift · git-hook
install state · lint · tests · CI conclusion · open PRs · cloud state ·
memory facts · check freshness. The CI/PRs/Cloud columns show the last-known
result cached by `ci`/`cloud-status` (they read the cache offline — only
those commands make network calls).

### Agent runs

`work` is the verb that turns a finding into a change. It launches a **headless**
agent against one project and bounds what it can do (ADR-007):

- It runs in a throwaway **git worktree**, never your working clone — so a project
  can be worked on while you are mid-edit in it.
- Its only sanctioned output is a **draft PR** on a new branch. It never pushes to
  `main`, never merges, never tags. That boundary is enforced here, in the run
  harness — not by the child's hooks, which are missing in exactly the projects a
  rollout targets.
- Its environment carries **no cloud credentials** — no
  `GOOGLE_APPLICATION_CREDENTIALS`, no `GH_TOKEN`, nothing. *That* is what puts
  the data plane out of reach, and it is worth being precise about why, because
  the comfortable version of this sentence is false: a `work` agent **does** get
  general `Bash` (it has to — running the project's own build and tests is most
  of a coding task). So it can invoke `gcloud`, or even `projects-orchestrator
  deploy`, and find nothing to authenticate with. The containment is the scrubbed
  environment and the draft-PR output boundary, **not** a narrow tool list. (Only
  `heal`, which is unattended and needs no shell beyond the declared gates, scopes
  `Bash` down to them.) Code is reversible; state is not.
- An agent that hits an ambiguity does not guess — it stops as `needs-human`, and
  `work <project> --attach` drops you into an interactive session in that same
  worktree with the context already loaded.
- Each run records **what it cost**. A run that was killed or timed out never
  reports its spend, so it shows as `—` and is counted separately — never summed
  as `$0.00`, because an unmetered run is unknown, not free.

A **campaign** is the same thing across the fleet: a declarative task plus a
selector (`--where ci=fail`, `scaffold=none`, `drift>0`) and a policy. It canaries
one project first and reports what the fan-out would cost at that rate; `--apply`
takes on the rest. In the controller REPL, `heal <project>` is the narrow
preset — spawn a scoped agent to fix a cached lint/test failure and open a PR.

The contract surfaces the orchestrator reads from each project-init child are
pinned in [`docs/reference/descriptor-contract-v1.md`](docs/reference/descriptor-contract-v1.md);
the additive v2 surfaces (deploy block, observability path, expected hooks) are
specified in [`docs/reference/descriptor-contract-v2.md`](docs/reference/descriptor-contract-v2.md).

`checks --changed-only` trusts a cached pass only for a project whose clean
worktree is still at the same HEAD commit — note it cannot see changes in
external services your tests hit, so run a plain `checks` when that matters.
`start` supervision state (pid, uptime, log) lives under `$XDG_STATE_HOME`
and feeds the status table's `Running` column.

In the TUI, selecting a row opens the per-project Detail tab (descriptor,
last-known checks, recent commits, memory) with `l`/`t` running that
project's lint/test gates. The controller's `/ask <question>` mode is
optional and off by default: set `FLEET_AGENT_ASK_MODEL` (a Claude model
id) and `ANTHROPIC_API_KEY` to let a model *select among the existing
commands* — the deterministic dispatcher still executes them.

### Which projects?

Copy [`fleet.yaml.example`](fleet.yaml.example) to `fleet.yaml` and list
scan roots / explicit paths. The same file can list extra `memory_sources` for
`memory` search, and a `host_health_command` whose first output line becomes the
fleet view's host tile (`host: unknown` until one is declared and answers), and a
`models:` table naming the model tier and effort each agent launch (`heal`, `work`,
`attach`) runs on. Without it, the orchestrator scans
`$PO_FLEET_ROOT` when that is exported, else the directory the repositories
live in side by side: `$PORT_ROOT`, else `~/port`. An empty `PORT_ROOT` counts
as unset, and with neither `PORT_ROOT` nor `HOME` set there is no default —
discovery scans nothing and says why rather than guessing `/port`.
Anything with a descriptor (`.agents/config.yaml` on a current scaffold,
`.claude/config.yaml` on a pre-PI-627 one — either is found) counts as a
project; the orchestrator only ever *reads* that contract.

Anything **without** a repo is invisible here by design — it has no descriptor,
no CI and no review, so there is nothing about it an agent could change safely.
`orphans` makes that estate at least *enumerable*: a read-only GCP inventory
diffed against the fleet. It holds no write credentials, and an unauthenticated
scan reports `unknown` rather than "no orphans" — falsely clearing an estate
nobody looked at is the worst lie it could tell.

### Every command

The complete registered surface — `projects-orchestrator --help` is the
authority, and this table is checked against it by a test, so a command added
without a row here fails CI.

**Read the fleet**

| Command | What it answers |
|---|---|
| `projects` | Which projects are governed, and where |
| `status` | Git health per project, or the whole fleet table |
| `doctor` | Does each project conform to the descriptor contract |
| `drift` | Has a project's scaffold diverged from its manifest |
| `audit` | The composed governance audit (`--digest` for deltas only) |
| `hardening` | Setup gaps, each with the command that closes it |
| `capabilities` | Which skills, hooks and MCP servers each project declares |
| `memory` | Search every project's memory at once, plus any `memory_sources` the fleet file lists |
| `history` | The recorded trend behind the status table |
| `events` | The machine-readable event stream |
| `snapshot` | The whole fleet as one JSON document |
| `upgrade-plan` | Who is behind upstream project-init |
| `orphans` | Cloud resources with no project claiming them |

**Act on it**

| Command | What it does |
|---|---|
| `checks` | Run each project's declared gates |
| `test-fleet` | Run every repo's `just test` and report one row per repo — passed, failed, exit — read from the test contract's `<suite>: N passed, M failed` line; exit 1 when any repo fails; a repo with no justfile or no `test` recipe is red, never skipped. Registered as the check `test.fleet`: every line carries `[check:test.fleet]` |
| `ci` | Probe CI conclusions and open-PR counts |
| `cloud-status` | Probe deployed services |
| `deploy` | Dispatch a project's deploy workflow (`--apply` to mean it) |
| `heal` | Open PRs that fix red gates |
| `campaign` | The same change across many projects, canary-first |
| `register` | Add a freshly-scaffolded project to the fleet |
| `notify` | Compute threshold alerts, optionally push them |
| `watch` | One scheduled pass: refresh, record, report alerts (emits, never pushes) |

**Agent runs and processes**

| Command | What it does |
|---|---|
| `work` | Launch an agent run in its own worktree |
| `start` / `stop` / `logs` | Supervise a project's long-running process |

**Surfaces**

| Command | What it is |
|---|---|
| `serve` | The live dashboard over HTTP |
| `tui` | The terminal UI |
| `controller` | The deterministic REPL |

### Design

See [`.agents/docs/adr/`](.agents/docs/adr/) — ADR-003 (fleet engine), ADR-005
(cloud control plane), ADR-006 (autonomous heal), ADR-007 (agent runs). Some
decisions are cited by number in code and docs but not yet written up; those are
tracked in #189 and marked where they are cited, so a dead link is never left
looking like a live one. In short:
the engine never raises (broken children degrade to `unknown`/`fail` cells, the
fleet view always renders), gates are the child's own declared commands,
last-known results persist under `$XDG_CACHE_HOME/projects-orchestrator/`, run
state under `$XDG_STATE_HOME/projects-orchestrator/`, and the controller is
deterministic — `/ask` may only *select among existing intents*, and it may
propose a `work` run but never launch one.

## Development

```sh
just setup    # uv sync --group dev
just test     # run the test suite
just lint     # ruff check
just ci       # what CI runs (lint + test)
```

Or directly:

```sh
uv sync --group dev --extra tui
uv run projects-orchestrator status
```

## License

Apache-2.0 — see [LICENSE](LICENSE).

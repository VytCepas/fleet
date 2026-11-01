---
kind: how-to
owner: @VytCepas
status: unreviewed
---

# Heal the fleet on a schedule

`heal` fixes a failing lint/test gate the way the engine detects one: it runs the
gate, hands a scoped coding agent the failing command and its error, re-runs the
gate to verify the fix, and — only on a verified pass — opens a **draft PR**. It
never touches a default branch and never merges anything (ADR-006). This guide
runs it across the whole fleet, unattended, on a timer.

Read this first, because unlike the digest, **an unattended heal spends money and
opens PRs**. Everything below is about keeping that bounded.

> **Platforms.** The timer that ships is a **systemd user unit**, so the install
> below schedules on Linux, and on WSL with systemd enabled. **No launchd plist
> ships, so on macOS nothing schedules this.** There `heal --all` runs only when
> you run it. "Unattended-safe" describes the spend cap and the draft-PR boundary,
> not a schedule that exists on every platform. The [cron form](#cron-instead) is
> the one route on a Mac, and it has not been tested there (#251).

## What one pass does

```bash
just heal-all                  # or: projects-orchestrator heal --all --limit 3
```

`heal --all`:

1. runs each project's `lint` and `test` gates (fresh, unless `--cached`),
2. picks the projects with a red gate, **up to `--limit`**,
3. heals them one at a time, opening a draft PR for each verified fix.

Projects past the limit are reported as `deferred` — named, never silently
dropped — so the cap is always visible. The closing line tallies what it healed
and what the agents cost:

```
alpha: fixed — PR opened at https://github.com/…/pull/42 (branch heal/lint-alpha-9f3c)
beta: verify_failed — still failing after the agent's fix: test (branch heal/test-beta-1a2b)
deferred 1 more (limit 3): gamma
healed 1/2 attempted — spend $1.34 across 2 runs
```

Exit codes suit a scheduler: **1** when the pass was *eventful* (it opened a PR, a
heal failed to verify, or a project was deferred), **0** when the fleet had
nothing to heal, **2** on a usage error.

## The safety model — why this is safe to leave running

- **Nothing merges.** A verified fix lands as a *draft* PR. The worst an
  autonomous pass can do is open a PR nobody wants.
- **Each agent is budget-capped.** A single heal's coding agent runs under a
  small per-run USD cap and a scoped tool allowlist (it can edit files and re-run
  *this project's own* declared lint/test commands, nothing else).
- **The pass is project-capped.** `--limit` bounds how many projects one firing
  spends an agent on, so a fleet that goes red overnight cannot fan out unbounded
  spend. Start low.
- **Only lint/test are eligible.** `ci`/`cloud` failures depend on remote state a
  local run can't reproduce, so they are never healed automatically.

Start with `--limit 1` or `2`, watch the PRs it opens for a few days, and raise it
only once you trust them.

## Install the timer

The unit heals `$PORT_ROOT` (else `~/port`) and attempts at most 3 projects per
firing. Override either in a private env file — and since a user unit reads no
shell profile, an exported `PORT_ROOT` belongs there too:

```bash
mkdir -p ~/.config/projects-orchestrator
printf 'PO_FLEET_ROOT=%s\nPO_HEAL_LIMIT=%s\n' "$HOME/projects" 2 \
  > ~/.config/projects-orchestrator/heal.env
```

Skip that step to accept the defaults (`$PORT_ROOT` else `~/port`, limit 3).

The same file takes `PO_HEAL_MODE=notify` to make the whole pass diagnose
instead of fix: no agent, no spend — each failing project is reported with
what failed and what to do next. Either way, a project's own descriptor may
declare `heal: { mode: fix | notify }` and that declaration wins over the
run's mode, in both directions (ADR-008). `--limit` caps *paid* attempts
only; notify-mode diagnoses are free and uncapped.

A heal opens a **draft** PR, and a draft notifies nobody. To hear about it, add
`PO_HEAL_WEBHOOK=<url>` (a Slack-compatible incoming webhook) to the same file.
Each eventful pass then posts one message naming every project it touched:
- a fix, with the PR URL and the gates it repaired;
- a failed heal, with the diagnosis;
- a notify-mode project, with what to do by hand;
- the projects the limit deferred.

A pass with nothing failing posts nothing. The PR stays a draft either way:
the notification replaces promoting it, which is what would put an agent's work
one auto-merge rule away from landing unreviewed.

A notify-mode project can report through its own repository instead. Add
`PO_HEAL_ISSUES=1` (the `--issues` flag) and each pass files **one GitHub issue
per failing gate** of every notify-mode project, on that project's repository,
with the failing command, the last line of its output (up to 200 characters)
and the next step. Only the value `1` enables it; `0` or `false` leave it off.

- a gate that is already reported files nothing new, however many passes it
  stays red;
- a second gate failing on the same project gets its own issue;
- once the pass runs the gate and it passes, the issue gets a closing comment
  and is closed;
- a skipped gate, or one this pass did not run, leaves its issue open.
- a gate that did not run at the commit `origin`'s default branch points at
  files and closes nothing: uncommitted or untracked changes, a local branch,
  or unpushed or unpulled commits describe your work, not the project. The heal
  report still shows the failure. The tip is asked of `origin` on every pass,
  so a checkout that has not pulled a newer push is skipped too, and an
  unreachable `origin` files and closes nothing.

The issues find each other by a hidden marker in their body, read from the
repository on every pass, so there is no state file to lose. Only issues opened
by the account `gh` is signed in as are read, so an issue someone else opened
with a copied marker is ignored. Only an issue
carrying that marker is ever closed, and the marker is re-read just before
closing. A read that fails files and closes nothing for that project, and the
pass logs `issues: delivery failed`. The evidence has your home directory
replaced with `~`, but it is still your gate's output: read it with the
repository's visibility in mind. Fix-mode projects never get an issue, and
their issues are never read: their report is the draft PR. So a project moved
from notify to fix keeps any issue it already has open until it returns to
notify mode or you close it.

`gh` must be authenticated for every repository in the fleet. Try it once by
hand on one notify-mode project before enabling it on the timer:

```bash
projects-orchestrator heal <project> --mode notify --issues
```

```bash
mkdir -p ~/.config/systemd/user
cp contrib/systemd/fleet-controller-heal.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now fleet-controller-heal.timer
```

Check it:

```bash
systemctl --user list-timers fleet-controller-heal.timer   # next firing
systemctl --user start fleet-controller-heal.service       # run it now
journalctl --user -u fleet-controller-heal.service -n 30   # what it did
```

The unit expects the console script at `~/.local/bin/projects-orchestrator` (a
`uv tool install` / `pip install --user` puts it there). Adjust `ExecStart` if
yours lives elsewhere.

### cron instead

```cron
PO_FLEET_ROOT=/home/you/projects

# Daily at 03:00. `heal --all` exits 1 on an eventful pass — that is a working
# run with something to review, so don't let cron treat it as an error.
0 3 * * * "$HOME/.local/bin/projects-orchestrator" heal --all --root "$PO_FLEET_ROOT" --limit 2 || true
```

cron runs with a near-empty environment, so set the variables at the top of the
crontab and use absolute paths. The agent still needs your `claude` and `gh`
credentials on `PATH` — see below.

## Why this runs on your machine, not in GitHub Actions

The same two reasons the audit digest can't:

1. **The fleet is local.** Discovery scans sibling directories on disk for
   `.agents/config.yaml`. The descriptor records no git remote, so a hosted
   runner has no way to find — or clone — the fleet it is meant to heal.
2. **It needs your credentials.** Heal spawns the `claude` CLI and opens PRs with
   `gh`; both authenticate as *you*, from your user session. A hosted runner has
   neither, and wiring them in would mean handing a cloud runner standing write
   access to every repo in the fleet — the opposite of the PR-gated,
   nothing-merges-itself boundary this feature is built on.

Both problems disappear when the heal runs where the fleet already lives and your
credentials already are. A self-hosted runner on that machine would work for the
same reason — the timer is just the smaller way to get there.

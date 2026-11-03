---
kind: how-to
owner: @VytCepas
status: unreviewed
---

# Watch the fleet on a schedule

`watch` is one pass of everything a scheduled observer needs: it runs the
declared gates fleet-wide, probes each project's CI (via the forge its host
names) and cloud state, merges everything into the checks cache, appends it
to history (the trend column), then computes threshold alerts from the
refreshed view and reports them. It chains what `checks`, `ci`, `cloud-status`
and `notify` do separately, so a timer needs a single invocation.

`watch` **emits and never pushes**: it has no `--webhook`. Its output is the text
or `--json` report and the exit code (below); whatever schedules it owns the
alert path, so a box keeps one notifier, not one per tool. A box with its own
alerting runs `watch --json --interval <seconds>` from its scheduler and turns
`alerts` and the exit code into its own notice.

> **Platforms.** The timer that ships is a **systemd user unit**, so the install
> below schedules on Linux, and on WSL with systemd enabled. **No launchd plist
> ships and this guide has no cron form**: on macOS the pass runs when whatever
> schedules your box runs it (a launchd job of your own), or when you run it. Without
> either, fleet health is pull-only and the heartbeat reads `never` or the time of
> your last manual pass. `notify --webhook` (#251) is the one command that pushes alerts.

```bash
projects-orchestrator watch --interval 3600 --root ~/projects
```

`--interval` is the schedule this pass runs on, and it is what lets `status`
and `notify` tell a stale watch from a fresh one. Omit it and the heartbeat
records no schedule, so staleness is reported as unjudgeable rather than
guessed — which means a timer that later stops firing raises nothing. The
shipped unit passes it (`PO_WATCH_INTERVAL`, default 3600, matching its
`OnCalendar=hourly`).

```bash
# omitted — the pass still runs, but its death would go unnoticed
projects-orchestrator watch --root ~/projects
```

Exit codes are scheduler-shaped: **1** means eventful (an alert fired — look
at it), **0** means quiet (within thresholds), **2** means no fleet was found
(a mispointed root is a misconfiguration, not a permanently quiet fleet).

## Install the timer

```bash
mkdir -p ~/.config/systemd/user
cp contrib/systemd/fleet-controller-watch.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now fleet-controller-watch.timer
```

Configuration is optional, via a private env file:

```bash
mkdir -p ~/.config/projects-orchestrator
printf 'PO_FLEET_ROOT=%s\n' "$HOME/projects" \
  > ~/.config/projects-orchestrator/watch.env
chmod 600 ~/.config/projects-orchestrator/watch.env
```

With no env file the unit watches `$PORT_ROOT` (else `~/port`) and prints
alerts only to the journal. A user unit reads no shell profile, so an exported
`PORT_ROOT` belongs in the env file too. As with the other timers, `loginctl enable-linger "$USER"` keeps the
schedule alive without an open session, and on WSL systemd must be enabled in
`/etc/wsl.conf` (`[boot] systemd=true`) first.

## Why hourly is affordable

The unit passes `--changed-only`: a gate whose last cached **pass** is at the
project's current clean HEAD is reused, so an untouched healthy project costs
nothing per firing. Fails are never reused — a red gate reruns every hour, so
recovery is noticed within one firing. Drop the flag from your copy of the
service file to force every gate to run from scratch each time.

The CI and cloud probes always run regardless of `--changed-only` — an
unchanged local HEAD says nothing about a forge or a deployment, whose state
moves without any local edit. They stay cheap: a couple of forge calls per
project, and `deploy: none` projects cost nothing.

## Check it

```bash
systemctl --user list-timers fleet-controller-watch.timer
journalctl --user -u fleet-controller-watch.service -n 30
```

An eventful pass logs the alerts and exits 1 — the unit counts that as
success (`SuccessExitStatus=1`); a *failed* unit means the pass itself broke,
usually a mispointed `PO_FLEET_ROOT` (exit 2).

## How it relates to the other timers

The three ship as a set: the hourly **watch** notices (this guide), the daily
[digest](scheduled-audit-digest.md) summarizes what changed, and the daily
[heal](scheduled-heal.md) opens PRs that fix what watch noticed. Each is
independent — install any subset.

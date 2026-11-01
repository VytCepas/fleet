---
kind: how-to
owner: @VytCepas
status: unreviewed
---

# Run the audit digest on a schedule

`audit` prints the whole governance table every time. On a cadence you want the
opposite: **only what changed**. `audit --digest` diffs this run's
attention-worthy findings against the last run's and reports what is *new* and
what has *resolved* — a short note you can read in five seconds, or push to
Slack.

This guide sets it up to run daily, unattended.

> **Platforms.** The timer that ships is a **systemd user unit**, so the install
> below schedules on Linux, and on WSL with systemd enabled. **No launchd plist
> ships, so on macOS nothing schedules this.** There the digest runs only when you
> run it, and fleet health is pull-only. The [cron form](#cron-instead) is the one
> route on a Mac, and it has not been tested there (#251).

## Try it first

```bash
just digest                     # or: projects-orchestrator audit --digest
```

The first run has no prior state, so everything currently wrong is "new". Run it
twice — the second run should say `audit digest: no change since last run`. That
is the behaviour a timer depends on.

Exit codes suit a scheduler: **1** when there are new findings, **0** when
nothing new appeared.

## Add a Slack sink (optional)

```bash
projects-orchestrator audit --digest --webhook https://hooks.slack.com/services/...
```

The payload's top-level `text` key is what Slack renders; the `new` / `resolved`
arrays carry the machine-readable delta for any other consumer. Delivery is
best-effort and never raises — a dead webhook prints `webhook: delivery failed`
to stderr and the digest still reports normally.

Only a **changed** digest is posted. A daily job that said "no change" every
morning would train you to ignore the channel.

## Install the timer

The webhook URL is a credential, so it goes in a private env file rather than
the unit:

```bash
mkdir -p ~/.config/projects-orchestrator
printf 'PO_DIGEST_WEBHOOK=%s\n' 'https://hooks.slack.com/services/...' \
  > ~/.config/projects-orchestrator/digest.env
chmod 600 ~/.config/projects-orchestrator/digest.env
```

Skip that step to run with no sink — the digest still lands in the journal.

The unit audits `$PORT_ROOT`, else `~/port`, by default. If your fleet lives
elsewhere, add `PO_FLEET_ROOT=/path/to/your/projects` to that same file — and
put `PORT_ROOT` there too if you export one, because a user unit reads no
shell profile. Nothing else needs editing — the unit does not care where the
orchestrator itself is checked out.

```bash
mkdir -p ~/.config/systemd/user
cp contrib/systemd/fleet-controller-digest.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now fleet-controller-digest.timer
```

Check it:

```bash
systemctl --user list-timers fleet-controller-digest.timer   # next firing
systemctl --user start fleet-controller-digest.service       # run it now
journalctl --user -u fleet-controller-digest.service -n 20   # what it said
```

The unit expects the console script at `~/.local/bin/projects-orchestrator` (a
`uv tool install` / `pip install --user` puts it there). Adjust `ExecStart` if
yours lives elsewhere.

### cron instead

```cron
PO_FLEET_ROOT=/home/you/projects
PO_DIGEST_WEBHOOK=https://hooks.slack.com/services/...

# Daily at 09:00. `audit --digest` exits 1 on new findings — that is a working
# run with something to say, so don't let cron treat it as an error.
0 9 * * * "$HOME/.local/bin/projects-orchestrator" audit --digest --root "$PO_FLEET_ROOT" --webhook "$PO_DIGEST_WEBHOOK" || true
```

cron runs with a near-empty environment, so set the variables at the top of the
crontab and use absolute paths. Pass `--root` explicitly rather than relying on
a working directory — the fleet root is what the digest actually needs.

## Why this runs on your machine, not in GitHub Actions

The natural instinct is a scheduled workflow. It cannot work, for two reasons
that are worth understanding before you try:

1. **The fleet is local.** Discovery scans sibling directories on disk for
   `.agents/config.yaml`. The descriptor contract records no git remote for a
   child project, so a hosted runner has no way to find — or even clone — the
   fleet it is meant to audit. Teaching it to would mean adding repo URLs to the
   producer→consumer contract, which project-init ADR-025 §1 deliberately keeps one-way and
   pull-only.
2. **The delta needs memory.** The digest compares against state under
   `$XDG_STATE_HOME`. A hosted runner starts empty every run, so *every* finding
   would report as new, every time, and the job would always exit 1 — a digest
   that says "everything is new" daily is worse than no digest.

Both problems disappear when the digest runs where the fleet already lives: the
projects are on disk, and the state persists between runs. A self-hosted runner
registered on that machine would work for the same reason — the timer is simply
the smaller way to get there.

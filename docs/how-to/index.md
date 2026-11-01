---
kind: how-to
owner: @VytCepas
status: unreviewed
---

# How-to guides

Goal-oriented recipes for someone already working with the project: "How do
I X?" Each guide solves one real problem, assumes basic familiarity, and
skips teaching.

- [Install and upgrade the orchestrator itself](install-and-upgrade.md) — from a
  checkout, and why a pull is not an upgrade until the tool is reinstalled. Not
  `upgrade-plan`, which upgrades the child projects.
- [Watch the fleet on a schedule](scheduled-watch.md) — hourly, refresh the
  gates, record the trend, and push threshold alerts the moment they appear.
  Linux/systemd; nothing schedules it on macOS.
- [Run the audit digest on a schedule](scheduled-audit-digest.md) — report only
  what changed in the fleet, daily, with an optional Slack sink. Linux/systemd,
  or cron (untested on macOS).
- [Heal the fleet on a schedule](scheduled-heal.md) — open PRs that fix red
  lint/test gates, unattended, with a hard cap on how much one pass may spend.
  Linux/systemd, or cron (untested on macOS).
- [Operate the orchestrator's own state](operations.md) — what grows, what is
  safe to delete, what to back up, and why upgrading needs no migration step.
- [Run the dashboard as a service](serve-dashboard.md) — the live fleet view
  always on, loopback-bound, restarting itself, reachable from a phone over a
  tunnel.

---
kind: how-to
owner: @VytCepas
status: unreviewed
---

# Operate the orchestrator's own state

Everything the orchestrator remembers between runs lives in two directories, and
this page is about keeping them healthy: what grows, what is safe to delete, what
to back up, and what happens when a file moves between versions.

None of it is precious in the sense a database is — every file here is a *cache
of something re-derivable* — but "re-derivable" is not the same as "free", and
one of these files is load-bearing in a way the others are not.

## What lives where

Both roots honour the XDG variables, so a per-machine override moves everything
at once.

| File | Directory | What it is | Safe to delete? |
|---|---|---|---|
| `checks.json` | `$XDG_CACHE_HOME/projects-orchestrator/` | Last known gate results per project/task | Yes — the next `checks` run refills it |
| `history.jsonl` | `$XDG_STATE_HOME/projects-orchestrator/` | Bounded append log behind the trend column | Yes — trends restart from empty |
| `audit-digest.json` | `$XDG_STATE_HOME/projects-orchestrator/` | Baseline for `audit --digest` deltas | **Careful** — see below |
| `watch-heartbeat.json` | `$XDG_STATE_HOME/projects-orchestrator/` | When the scheduled pass last ran | Yes — reads as `never` until the next pass |

Defaults when the variables are unset: `~/.cache/…` and `~/.local/state/…`.

**`audit-digest.json` is the one to think about before deleting.** It is the
*previous* set of findings, and `audit --digest` reports the delta against it.
Delete it and the next digest treats every existing finding as new — which, if a
webhook is configured, means pushing a wall of alerts for problems nobody just
introduced. Deleting it is a reasonable way to force a full re-report; doing it
by accident is a bad afternoon.

## Growth and rotation

`history.jsonl` is the only file that grows with time rather than with fleet
size, and it is **self-bounding**: `record` truncates to the newest
`MAX_ENTRIES` on every write, so it cannot grow without limit and needs no
`logrotate` entry. Nothing here appends unboundedly.

The two caches grow with the number of projects and tasks, not with the number
of runs — a fleet of twenty projects produces a file measured in kilobytes.

**Supervised-process logs are the exception, and they are not per-run.**
`_log_file` is keyed by *project name alone* and `start` opens it with `"ab"`, so
every restart appends to the same file and it grows without bound for as long as
the service is noisy.

Two consequences that rule out the obvious recipe:

- **Age-based deletion never fires on the log that needs it.** An active service
  refreshes the mtime continuously, so `find -mtime +30 -delete` skips exactly
  the file that is growing.
- **It can unlink a log a live process still holds open.** A quiet but running
  service keeps writing to an inode with no name, so the bytes are lost and the
  space is not reclaimed until it exits.

Rotate by truncating in place, which keeps the descriptor the running process
holds valid:

```bash
LOG="${XDG_STATE_HOME:-$HOME/.local/state}/projects-orchestrator/<project>.log"
cp "$LOG" "$LOG.1" && : > "$LOG"      # copytruncate, safe while the process runs
```

Or stop the service first, which is the only way to rotate without a window
where writes land in the copy:

```bash
projects-orchestrator stop <project>
mv "$LOG" "$LOG.1"
projects-orchestrator start <project>
```

A `logrotate` entry wants `copytruncate` for the same reason.

## Back up

The short answer: **back up nothing, except possibly the digest baseline.**

Everything else is re-derived by running the gates again, and a restored
`checks.json` is *worse* than an empty one — it asserts results that were true on
another machine at another commit, and the status table would present them as
this machine's. An empty cache reads honestly as "never probed".

The one file worth keeping, if you push audit deltas to a webhook:

```bash
cp "${XDG_STATE_HOME:-$HOME/.local/state}/projects-orchestrator/audit-digest.json" \
   ~/backup/
```

**The fleet definition is the thing that actually matters, and it is not here.**
`fleet.yaml` lives beside the orchestrator, not in the state directory, and it is
the only file whose loss changes what the fleet *is* rather than what is known
about it. Keep that under version control or in your dotfiles; the rest of this
page is about disposable state.

## Migrating between versions

The cache carries a `__schema_version__` key and the reader reports four
verdicts, which is what makes an upgrade — or a rollback — safe:

- **Same version** — read normally.
- **Older, pre-envelope** — read, and re-stamped on the next write. No results
  are lost on upgrade.
- **Newer** — refused, and **not overwritten**. A build that does not understand
  the file leaves it for the build that does, rather than replacing it with its
  own narrower view. This is what makes running two installations against one
  `$XDG_CACHE_HOME` safe.
- **Unreadable** — treated as empty, and reported as corrupt rather than as
  version skew, because the remedies differ.

So the migration procedure is: **there isn't one.** Upgrade, downgrade, or run
two versions side by side; no file needs converting and nothing is silently
discarded.

The one thing to know: after a rollback, the older build drops the version key
when it writes. The newer build reads that back as pre-envelope and re-stamps it.
Neither direction loses a result.

## Starting clean

**Do not `rm -rf` the state directory.** It is not only caches: `work` places
each agent run's git worktree under
`$XDG_STATE_HOME/projects-orchestrator/worktrees/`, alongside its run record in
`runs/` and its briefing. Deleting the tree therefore destroys **uncommitted
changes in a real checkout**, strands any live agent, and leaves dangling git
worktree metadata in the project repo. The orchestrator never writes *into* the
repos it reads (ADR-003) — but a worktree it created is project data by any
useful definition.

Reset the derivable state only, naming each file rather than sweeping the
directory:

```bash
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/projects-orchestrator"
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/projects-orchestrator"
rm -f  "$STATE/history.jsonl" "$STATE/audit-digest.json" "$STATE/watch-heartbeat.json"
```

The next `checks` run rebuilds the cache, and the first `audit --digest`
afterwards reports every finding as new, for the reason above.

To clear agent runs as well, take them through their lifecycle first so the
worktrees are removed from git's metadata rather than orphaned:

```bash
projects-orchestrator work --list            # what is still live
projects-orchestrator work --stop <run-id>   # kill a running agent, mark it abandoned
projects-orchestrator work --clear <run-id>  # forget a settled run and release its worktree
```

`--clear` is the one that matters here: it is what removes the worktree through
git rather than leaving the repo with metadata pointing at a directory that no
longer exists.

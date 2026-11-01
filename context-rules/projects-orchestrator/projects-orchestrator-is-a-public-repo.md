---
order: 10
event: SessionStart
when: always
once: never
---

**This repository is PUBLIC on GitHub — a commit, an issue and a PR body all
publish.** The rule itself is `AGENTS.md`, "Key rules for agents", first bullet:
what must never be published, the deliberate exception for this package's own
`authors` metadata, and why `fleet.yaml` is the load-bearing one. Read it before
the first publishing action of the session.

This file exists only to make that rule RESIDENT, and it carries no copy of it.
Two reasons, both learned here:

- **It must arrive before the publishing action, not with it.** This fired on
  `cmd-contains git` AND `cmd-contains commit` first — `when` repeats are ANDed —
  so it was silent for `gh issue create` and `gh pr create`, and the documented
  workflow here opens an issue *before* the first commit. The warning arrived
  after the publish it existed to prevent. There is no OR in the predicate
  vocabulary, so enumerating publishing paths means one pack per path; being
  resident from the first turn covers the ones nobody enumerated (#232).
- **It must not be the only copy.** A pack under `.claude/` is loaded by Claude
  and by nothing else, so on any other agent surface the rule would simply not
  exist. `AGENTS.md` is the file every agent reads, which is why the rule lives
  there and a pointer lives here (#231).

A rule stated in two places drifts in one of them, and the copy that drifts is
the one nobody is reading when it matters.

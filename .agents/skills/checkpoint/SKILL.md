---
name: checkpoint
description: Checkpoint-and-clear — write a structured session handoff to a gitignored file so the user can /clear and resume from ~500 tokens instead of a re-sent long context
when_to_use: Use when a session is getting long (context above ~60%), before switching to an unrelated task, or when the user says "checkpoint", "save state and clear", or "resume from checkpoint".
user-invocable: true
---

# Checkpoint — save state, clear, resume cheap

Every turn re-sends the whole conversation. Deep into a session you pay for
100k+ tokens of history on every exchange; after a checkpoint-and-clear, the
next session starts from a ~500-token handoff file instead. Unlike
auto-compaction, *you* choose what survives — nothing load-bearing is
summarized away.

## Writing a checkpoint

1. **Verify the path is ignored before writing** — projects scaffolded before
   this skill existed may lack the ignore entry, and the file contains
   session state that must never be committable:

   ```bash
   git check-ignore -q .agents/tmp/checkpoint.md 2>/dev/null \
     || printf '\n# Session handoff files (checkpoint skill)\n.agents/tmp/\n' >> .gitignore
   ```

   Then write the handoff to `.agents/tmp/checkpoint.md` (create the
   directory if needed).
2. Use exactly this structure — every section, one line each unless more is
   genuinely load-bearing:

   ```markdown
   # Checkpoint <ISO date>
   ## Task
   <issue number + one-line goal>
   ## Done (verified)
   <only work backed by a passing test or completed command>
   ## Decisions
   <choices made and WHY — the part a fresh session can't rediscover>
   ## Next steps
   <ordered, concrete — first item should be startable immediately>
   ## Open files / branch
   <branch name; files mid-edit with line areas>
   ## Test state
   <last suite result; known failures>
   ```

3. Keep it under ~60 lines. If a section would be long, that content belongs
   in a commit, an ADR, or memory — not the checkpoint.
4. Tell the user: "Checkpoint written to `.agents/tmp/checkpoint.md` — run
   `/clear`, then say **resume from checkpoint**."

## Resuming

1. Read `.agents/tmp/checkpoint.md`. Treat **Decisions** as settled — do not
   re-litigate them.
2. Verify the branch and test state match the file (one `git branch
   --show-current` + the fail-fast test run) before continuing Next steps.
3. **Offer to delete the file** once resumed and verified: it is stale the
   moment work continues, and a stale checkpoint silently misleads the next
   resume. Delete it (`rm .agents/tmp/checkpoint.md`) unless the user wants
   it kept.

## Checkpoint vs the alternatives

- `/compact` — lossy automatic summary; fine mid-task, but you don't control
  what survives.
- `session-summary` skill — archival record for the vault; not optimized for
  continuation.
- `checkpoint` — continuation-optimized, human-auditable, cheapest resume.

---
name: session-summary
description: Summarizes the current session and saves it to the vault. Use at the end of a work session to record completed work, decisions made, and open items for the next session.
when_to_use: Use when the user says "save the session", "wrap up", "summarize what we did", or at the end of a long work session.
effort: medium
allowed-tools: Bash(git *) Read Write Glob Grep
---

Summarize this session and save it:

**First check where to save.** This skill targets the Obsidian vault. If `.agents/vault/` does not exist, this project has no vault (memory tier is `none` or `auto`) — do **not** create it; instead save to `.agents/memory/` if that exists, or otherwise just print the summary to the user without writing a file.

1. **Gather context**:
   - Run `git log --since='2 hours ago' --oneline` for recent commits
   - Run `git diff --stat` for uncommitted changes
   - Review the conversation for key decisions and discoveries

2. **Write the summary** to `.agents/vault/sessions/` with today's date (or the fallback location above):
   ```
   # Session YYYY-MM-DD (manual)

   ## What was done
   - (bullet list of completed work)

   ## Decisions made
   - (any architectural or approach decisions, with reasoning)

   ## Open items
   - (anything left unfinished or discovered but not addressed)

   ## Notes
   - (anything else worth remembering)
   ```

3. **Update memory** if any reusable facts emerged — only if `.agents/memory/` exists (write to the project memory directory)

Keep the summary concise — a future agent should be able to skim it in 30 seconds.

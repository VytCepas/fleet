# projects-orchestrator — Claude Code entry point

@AGENTS.md

That bare line is an IMPORT and has to stay bare. Claude Code expands `@path`
and nothing else, so a markdown link to the same file loads nothing — and this
file existing at all stops Claude reading `AGENTS.md` natively. Backticks or a
code fence disable the import the same way.

`AGENTS.md` is the source of truth: workflow, conventions, memory, tools, branch
naming, and the Claude-only details in its `## Claude Code specifics` section.
Claude-specific content belongs there, not here — only what `AGENTS.md` cannot
carry, like the loading facts and compaction directives below, lives in this file.

## How Claude Code loads this project

- **Only in a session started here.** Run `claude` from this directory. A session started anywhere else (your home directory, a parent folder) loads none of this project's settings, hooks or plugins.
- **From `.claude/`, not `.agents/`.** Claude Code reads its config from `.claude/`, a copy generated from `.agents/`, and does not read `.agents/` itself; other agents do (see `AGENTS.md`).
- **Skills and hooks come from project-scoped plugins**: `project-init-workflow` and `project-init-lifecycle`, enabled for this repository only, which is why `.claude/` carries none of their skills. To have them in every session, install them at user scope: `claude plugin marketplace add VytCepas/project-init`, then `claude plugin install project-init-workflow@project-init --scope user` and the same for `project-init-lifecycle@project-init`. Their hooks then run in every repository you open.

## Compact Instructions

When compacting this conversation, preserve:
- The project ticket key and GitHub issue number being worked on (e.g. `PI-42`, GitHub `#42`)
- Files modified in this session (list by path)
- Test results: pass/fail count and any failing test names
- Unresolved errors or lint failures
- Any decisions made that aren't yet committed to `.agents/docs/decisions/`

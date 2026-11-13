# ADR-002: MCP Choices

**Date:** 2026-06-28
**Status:** Accepted

## Context

Model Context Protocol (MCP) servers extend Claude Code's toolset. Each server loads its full tool-definition set into the conversation on startup — even tools never used. This adds token overhead proportional to the number of tools.

## Decision

The following MCPs are intentionally absent — to reduce token overhead, and (for the DB MCPs) to avoid an unmaintained, CVE-prone dependency:

| MCP | Tokens saved | Replacement |
|---|---|---|
| GitHub MCP (~35 tools) | ~2000 tokens | `gh` CLI built-in |
| Linear MCP (~15 tools) | ~800 tokens | `gh` CLI + GitHub Issues |
| Filesystem MCP (~10 tools) | ~500 tokens | Claude Code built-in Read/Write/Edit/Glob/Grep |
| Postgres / SQLite DB MCP | — | native `psql`/`sqlite3` via Bash |

The DB MCPs were removed in PI-387: the reference servers (`@modelcontextprotocol/server-postgres`, `mcp-server-sqlite`) were archived with unpatched SQL-injection CVEs, and a DB MCP overlaps with the agent's native shell access — the same rationale as Filesystem above. A project that wants structured, guard-railed DB access can add a maintained server (e.g. `@bytebase/dbhub`) itself.

**Selected at init:** none

Retained MCPs:
- **Context7** — targeted library doc lookups; no CLI equivalent. Default install is stdio (`bunx`). For Claude web/mobile/Cowork (HTTP MCP only — stdio servers are invisible there) select the `context7-http` catalog entry instead, which emits the hosted Streamable-HTTP endpoint `https://mcp.context7.com/mcp` (PI-397).
- **Playwright** — browser automation; no CLI equivalent

## Consequences

- Agents use `gh issue list`, `gh pr create`, etc. for project management
- File ops use Claude Code built-ins — no MCP config needed
- Token budget stays ~3300 tokens lower per session; more context for actual work
- `MCP_CATALOG` in project-init contains only context7 (plus the optional Playwright browser MCP)

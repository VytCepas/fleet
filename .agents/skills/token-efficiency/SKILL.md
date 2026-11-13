---
name: token-efficiency
description: Work token-frugally during coding and debugging sessions — keep tool output, file reads, and your own responses from bloating the context window
when_to_use: Load at the start of any substantive coding, debugging, or investigation session, or when a session is getting long and context is filling up.
user-invocable: true
---

# Token-efficient working habits

Everything that enters the transcript — tool output, file contents, your own
prose — is re-sent to the model on **every subsequent turn** until the session
is cleared or compacted. A 5k-token test dump ingested on turn 3 of a 40-turn
session is paid ~37 times. The cheapest token is the one that never enters the
transcript.

## Input side — filter before ingest

**Commands: make the tool do the filtering.**

- Tests: use the fail-fast quiet recipe while iterating — `just test-quick`
  (stops at the first failure; one traceback, not the whole suite's). Run the
  full `just test` once, for the final green check.
- Gates: batch them — one `just ci` (or `just lint` + full test) exactly once
  before finishing. Re-running whole-project gates after every edit pays the
  full report each time; fix lint findings per-file as they are reported.
- Noisy commands: pipe before ingesting — `… 2>&1 | tail -n 40`,
  `… | grep -E "FAILED|ERROR"`. Ask for the slice you need, not the firehose.
- Diffs: `git diff --stat` first; open the full diff only for the files you
  actually need, one file at a time (`git diff -- <path>`).
- Never run streaming/watch commands in the transcript (`--watch`, `tail -f`,
  poll loops). Run them detached with output to a file, then read the tail.

**Reads: ranged and purposeful.**

- Read line ranges, not whole files, when you know roughly where the target is
  (search first, then read the matching region).
- Check the cheap indexes before grepping the tree: `code-map.md`, the memory
  index (`.agents/memory/MEMORY.md`), and `docs/` — each where present (not
  every scaffold ships every index).
- Don't re-read a file you just edited — the edit either applied or errored.
- When a tool result says it was compressed or truncated and points at a spill
  file (e.g. `.agents/tmp/tool_output/…`), Read only the line ranges you need
  — re-reading the whole file pays back the cost the compression just saved.

**Searches: delegate sweeps, not lookups.**

- For broad multi-file searches ("where is X handled?", naming-convention
  hunts), use the built-in `Explore` subagent: the file
  dumps stay in the subagent's context and only the conclusion returns.
- Orientation contract (#687, was explore.md's job before PI-848): when
  delegating a sweep, tell the agent to consult `.agents/docs/code-map.md`,
  `.agents/memory/MEMORY.md`, and `.agents/CAPABILITIES.md` first — each
  may be absent. Verify in the source before asserting any specific value;
  a mapped path that no longer exists means the map is stale. Report the
  staleness you found alongside the answer.
- Caveat: a subagent costs ~4× the tokens of doing it inline — it saves your
  *main context*, not total spend. For a single-file lookup where you know the
  symbol, just search directly.

## Output side — spend words where they change decisions

- Give explanations specific budgets: summaries ≤ 3 sentences, one reason per
  rejected alternative. Specific budgets work; "be concise" doesn't.
- Never restate unchanged code. Show the edited hunk or describe the change;
  don't re-print a file to prove an edit landed.
- Prefer tables/lists only for genuinely enumerable facts; skip decorative
  structure.

## Instruction files — keep the always-loaded layer thin

- CLAUDE.md: under 200 lines (official guidance). Per line ask: "would removing
  this cause a mistake?" If not, cut it.
- SKILL.md bodies: under 500 lines. Skills cost ~60–100 tokens of metadata
  until loaded — push sometimes-relevant knowledge into skills instead of
  always-loaded instructions.
- Splitting CLAUDE.md with `@import` does NOT save tokens — imports load
  eagerly.

## Knobs (reference — defaults are usually right)

These Claude Code environment variables cap tool output with overflow-to-file
(nothing is lost, it just stays out of the transcript). Defaults are sane;
override in `.agents/settings.json` `env` only with a measured reason:

| Variable | Caps | Default |
|---|---|---|
| `BASH_MAX_OUTPUT_LENGTH` | Bash output chars | 30,000 |
| `MAX_MCP_OUTPUT_TOKENS` | MCP tool response tokens | 25,000 |
| `TASK_MAX_OUTPUT_LENGTH` | Subagent final-output chars | 32,000 |
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | Autocompact trigger (already set to 70 in this scaffold) | model-dependent |
| `PI_COMPRESS_MIN_CHARS` | `tool_output_compressor` hook threshold — an unfiltered `git diff`/`show`/`log` result above it is replaced by a diffstat + pointer to the full text in `.agents/tmp/tool_output/` (`PI_COMPRESS_TOOL_OUTPUT=0` disables) | 4,000 |

If the observability overlay is installed, `usage_report.py` shows where the
session's tokens actually went — measure before tuning.

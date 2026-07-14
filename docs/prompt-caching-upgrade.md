# Prompt Caching Upgrade — Implementation Note

**Status:** Implemented 2026-07-03 (plan: `docs/plans/2026-07-03-prompt-caching-upgrade.md`).
Verified live against the Anthropic API: cold write 22,047 tokens → warm read 22,047 at 0.1× →
appended turn wrote only its 17-token delta. Full numbers: `docs/audits/live-test-log.md`
([2026-07-03 run 1](audits/live-test-log.md#run-2026-07-03-1)). In-app observation of the
`prompt cache:` log lines pending the next interactive session.
Originally decided 2026-07-02; deliberately excluded from the claude_tools.py audit
(`ibkr_core_mcp/docs/2026-07-02-claude-tools-audit-design.md`) so it could be implemented
independently from this note.

## Problem (confirmed 2026-07-02)

`claudia/agent.py` uses **no prompt caching** — there is no `cache_control` anywhere in the
codebase. Every `client.messages.stream()` call (agent.py:335) re-processes the full static
prefix uncached:

- the entire `tools=` payload from `_all_tools` (agent.py:296): 42 ibkr_core_mcp toolkit
  schemas + TradingView `extra_tools` + `_LOCAL_TOOLS`,
- the full system prompt.

This happens on **every user message and again on every tool-loop turn** (each
stream → tool_use → tool_result round-trip is a fresh API call with the same prefix). Cost and
time-to-first-token both pay for the same static tokens repeatedly.

## Change

Add `cache_control: {"type": "ephemeral"}` markers so the static prefix is cached:

1. **Tools** — on the **last** tool dict in the list returned by `_all_tools`
   (agent.py:296). Marking the last tool caches the entire tools array.
2. **System prompt** — convert `system=` from a plain string to the block form and mark the
   last block:
   ```python
   system=[{"type": "text", "text": system_prompt,
            "cache_control": {"type": "ephemeral"}}]
   ```

Cache hierarchy is `tools → system → messages`, so these two markers cache everything static.
No other architecture change.

## Facts verified against official docs (2026-07-02)

Source: https://platform.claude.com/docs/en/docs/build-with-claude/prompt-caching
(docs.anthropic.com redirects here — 301 as of 2026-07-02)

- Syntax: `"cache_control": {"type": "ephemeral"}` on individual content blocks; last-block
  marker caches everything before it in the hierarchy.
- TTL: 5 minutes default, refreshed at no extra cost on each use within TTL. (`"ttl": "1h"`
  exists at 2× write cost — not needed for an interactive chat that calls every few seconds.)
- Pricing: cache writes 1.25× base input; cache **reads 0.1×** (90% cheaper). For the default
  `claude-opus-4-8` ($5/MTok input): reads cost $0.50/MTok.
- Minimum cacheable length for `claude-opus-4-8`: **1,024 tokens** — the ClaudIA prefix
  (42+ tool schemas + system prompt) far exceeds this. Note: below-minimum prompts fail
  silently (no error, just no caching) — hence the verification step below.
- Streaming: fully compatible; usage arrives in the `message_start` event.
- Invalidation: any change to tool definitions invalidates the whole cache; `tool_choice`
  changes invalidate the message cache. The TradingView bridge swapping tools mid-session
  (`set_tv_bridge`, agent.py:289) will therefore invalidate — expected, harmless.

## Verification (do not skip)

After the change, confirm caching is actually active — check `usage` on the `message_start`
event across two consecutive messages:

- First call: `cache_creation_input_tokens` > 0 (prefix written).
- Second call within 5 min: `cache_read_input_tokens` > 0 and roughly equal to the prefix
  size; `input_tokens` drops to only the non-cached remainder.
- Both fields 0 → caching silently failed; re-check block placement.

Worth logging these two usage fields permanently — they are the cheap, always-on signal that
caching stays healthy as tools evolve.

## Expected impact

- Input-token cost for the static prefix drops ~90% on every cached call (all tool-loop turns
  and every message within the 5-minute window).
- Time-to-first-token improves on cached calls (prompt processing skips the cached prefix).

## Implementation findings (2026-07-03)

Full evidence trail: `docs/audits/2026-07-03-llm-best-practices-sources.md` (claim→source table C1–C13,
three consistency rounds). Findings beyond this note's original scope:

1. **Last-tool marker must copy, not mutate.** `_all_tools` concatenates shared dicts
   (`_LOCAL_TOOLS`, ibkr_core_mcp `TOOL_DEFINITIONS`); in-place `cache_control` would
   permanently alter the module constants. `_with_cache_marker` copies the last dict;
   regression tests assert the constants stay clean.
2. **Context/principles hot-reload invalidates the system+messages cache** — expected,
   same category as the TV-bridge tool swap. Tools cache survives both (invalidation
   hierarchy). Both show up as a one-time `created>0` bump in the `prompt cache:` log line.
3. **Third breakpoint on the final message content block** (beyond this note's
   "no other architecture change") — without it, the growing conversation was re-processed
   at full price on every tool-loop turn. Rebuilt per call on a copy; 3 of 4 breakpoints
   used. Caveats: (a) a single turn adding >20 content blocks (10+ parallel tool calls)
   exceeds the 20-block lookback window and re-writes instead of reading — visible in the
   log line; (b) once history exceeds `_HISTORY_LIMIT=40` rows the sliding window shifts
   the messages prefix and the messages cache misses once per user turn (tools+system
   unaffected; hysteresis eviction is an optional follow-up if logs show it matters);
   (c) a new user turn resumes from the cache entry at the end of the *reconstructed*
   history, not from mid-tool-loop entries (tool rows are skipped on rebuild).
4. **System prompt is now built once per session** (user decision 2026-07-03: doc-version
   and document checks happen at load, never per prompt). `ContextLoader.reload_count` is
   bumped by the watchdog; `ClaudIAAgent._get_system_blocks` rebuilds only when it changes.
   Per-prompt cost dropped from 2 file reads + 1 `doc_versions` query to one int comparison.

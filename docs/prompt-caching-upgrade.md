# Prompt Caching Upgrade — Implementation Note

**Status:** To do. Decided 2026-07-02; deliberately excluded from the claude_tools.py audit
(`ibkr_core_mcp/docs/2026-07-02-claude-tools-audit-design.md`) so it can be implemented
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

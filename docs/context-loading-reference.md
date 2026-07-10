# context.md and principles.md — Loading & Versioning Reference

These two documents define ClaudIA's entire behavior. They are loaded at session start
and injected as the system prompt. **Never commit these files** — they contain your
personal trading rules.

- `docs/context.md` — Who ClaudIA is: its role, persona, areas of expertise, communication style.
- `docs/principles.md` — Your trading rules: risk limits, preferred strategies, instruments, position sizing, red lines.

**Hot-reload:** Edit and save either file while a session is running. ClaudIA will notify
you in chat and apply the new content from the next message onwards.

**Load-time resolution (not per-message):** `ClaudIAAgent._get_system_blocks()` builds the
system prompt **once per session** and caches it — document reads and the `doc_versions`
version-note query happen at that point, never on every prompt. The watchdog in
`ContextLoader` increments `reload_count` on every file-change event; the agent compares its
cached counter against the loader's on each message and rebuilds only when they differ.
Steady-state per-message cost is one integer comparison. This also guarantees the system
prompt is byte-identical across a session's tool-loop turns, which prompt caching depends on.

**Versioning:** On every session start, a SHA-256 hash of both documents is computed. If
the hash is new, it is automatically registered as the next version (`v1`, `v2`, …) in
`claudia.db → doc_versions`, and a human-readable snapshot is written to
`docs/versions/{label}/`. ClaudIA's system prompt always includes the active version label
so it knows which rules are in effect. If the version changed since the last session, a
`WARNING: v1 → v2` alert appears in chat.

ClaudIA has two tools to reason about version history:
- `list_doc_versions` — enumerate registered versions with dates
- `get_doc_version("v1")` — retrieve the full content of any past version to check for contradictions with current rules

Past conversation history retrieved from memory always includes which document version was
active, so ClaudIA can flag if something discussed under old rules conflicts with the current
principles.

## Prompt Caching (mechanics — design rationale lives in the superpowers plan doc)

`claudia/agent.py` marks three `cache_control: {"type": "ephemeral"}` breakpoints on every
`client.messages.stream()` call, following the prefix hierarchy `tools → system → messages`:

| Breakpoint | Helper | Caches |
|---|---|---|
| Last tool definition | `_with_cache_marker` | All tool schemas (`ClaudeToolkit` + TradingView `extra_tools` + `_LOCAL_TOOLS`) |
| System prompt (block form) | `_system_blocks` | Version note, context.md, principles.md, market calendar, `_SAFETY_BLOCK` |
| Last message content block | `_with_history_cache_marker` | Conversation history — rebuilt on a **copy** every call, never mutating the loop's working `messages` list |

**Why 3 breakpoints, not the note's original 2:** caching only tools + system left the
growing conversation re-processed at full price on every tool-loop turn. The messages
breakpoint closes that gap — each call reads the prior prefix at 0.1× and writes only the
newly appended blocks at 1.25×.

**Live-verified** (2026-07-03, exact production request shape): a ~22K-token static prefix
(46 tool schemas — 42 `ibkr_core_mcp` + 4 local — + system prompt) written once
(`cache_creation_input_tokens=22047`), then read at 0.1× on every subsequent call
(`cache_read_input_tokens=22047`); an appended assistant+user turn wrote only its 17-token
delta. Full numbers: `docs/live-test-log.md#run-2026-07-03-1`.

**Health telemetry:** every call logs `prompt cache: created=… read=… uncached=…` at INFO
(`_log_cache_usage`), with a WARNING if both `created` and `read` are zero — the silent-failure
signal for a below-minimum prefix (1,024 tokens on `claude-opus-4-8`) or a misplaced marker.

**What invalidates the cache** (tools cache survives; system+messages caches do not):
- Editing `context.md`/`principles.md` (hot-reload) or a doc-version change — expected, rare
- TradingView sidecar connect/disconnect (`set_tv_bridge` swaps `_extra_tools`)
- A single tool-loop turn adding more than 20 content blocks (10+ parallel tool calls) exceeds
  the 20-block lookback window and silently misses instead of reading — visible as
  `created>0 read=0` in the log line
- Once a session exceeds `_HISTORY_LIMIT=40` messages, the sliding history window shifts the
  messages prefix and that cache misses once per user turn (tools+system unaffected)

Full design rationale, source-verified claims, and the three-round consistency review:
`docs/superpowers/plans/2026-07-03-prompt-caching-upgrade.md` ·
`docs/2026-07-03-llm-best-practices-sources.md`.

Source: https://platform.claude.com/docs/en/build-with-claude/prompt-caching

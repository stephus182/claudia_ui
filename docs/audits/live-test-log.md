# Live Integration Test Log — claudia_ui

Accumulated record of machine-executed live tests against real external services
(Anthropic API, Google Drive, TradingView sidecar). Same convention as
`ibkr_core_mcp/docs/live-test-log.md`: every entry produced by an executed run —
no simulated responses.

When referencing a past live test, link here with an anchor, e.g.
`[2026-07-03 run 1](#run-2026-07-03-1)`.

---

<a id="run-2026-07-03-1"></a>
## Run: 2026-07-03 — prompt-caching live verification (Anthropic API)

| Field | Value |
|---|---|
| Date | 2026-07-03 |
| Purpose | Verify the 3-breakpoint prompt-cache implementation (plan: `docs/superpowers/plans/2026-07-03-prompt-caching-upgrade.md`, Task 6) |
| Method | Scripted `messages.create` calls using the **exact request shape ClaudIA sends**: `_with_cache_marker(TOOL_DEFINITIONS + _LOCAL_TOOLS)` (42+4 tools), `_system_blocks(_build_system_prompt(...))` with the real `docs/context.md`/`principles.md`, `_with_history_cache_marker(messages)` |
| Model | `claude-opus-4-8` |
| Result | **PASS** — all three checks |

### Findings

| Call | `cache_creation_input_tokens` | `cache_read_input_tokens` | `input_tokens` (uncached) | Verdict |
|---|---|---|---|---|
| 1 — cold, first message | 22,047 | 0 | 2 | Prefix written (tools + system + first message), well above the 1,024-token minimum |
| 2 — same prefix, within TTL | 0 | 22,047 | 2 | 100% cache hit — prefix read at 0.1× |
| 3 — appended assistant + user turn | 17 | 22,047 | 2 | Incremental history breakpoint: prior prefix read, only the 17 new tokens written |

### Interpretation

- Static prefix is ~22K tokens; every warm call now pays 0.1× on it instead of full price (~90% input-cost reduction on the prefix, exactly as the implementation note projected).
- Call 3 proves the moving messages breakpoint works: the previous entry is found via lookback and only the delta is written at 1.25×.
- No `prompt cache inactive` condition observed (both fields were never simultaneously zero).

### Remaining (in-app observation)

The same telemetry is logged on every call by `_log_cache_usage` (INFO `prompt cache: created=... read=... uncached=...`; WARNING if both zero). To be observed in the Chainlit UI during the next interactive live session — including a tool-call turn, where the second stream call of the loop should log `read > 0` covering tools + system + pre-tool conversation.

---

*To add a new run entry, prepend it above this one.*

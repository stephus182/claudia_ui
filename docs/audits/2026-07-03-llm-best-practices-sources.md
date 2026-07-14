# LLM Best-Practices — Authoritative Sources & Claim Verification

**Date:** 2026-07-03
**Purpose:** Per the project's docs-first rule ("never assume API behavior — always verify against official documentation"), this document records the scraped authoritative evidence behind every technical claim in:
- `docs/superpowers/plans/2026-07-03-prompt-caching-upgrade.md` (caching plan)
- `docs/2026-07-03-agent-info-architecture-review.md` (information-handling review, RAG design rules)

and the results of the consistency / logic-defect pass run against that evidence before implementation.

**Method:** Live WebFetch scrapes on 2026-07-03. Quotes are verbatim from the fetched pages.

---

## 1. Source registry

| ID | Source | URL | Authority | Scraped |
|---|---|---|---|---|
| S1 | Anthropic — Prompt caching | https://platform.claude.com/docs/en/build-with-claude/prompt-caching.md | Official API docs | 2026-07-03 |
| S2 | Anthropic — Streaming Messages | https://platform.claude.com/docs/en/build-with-claude/streaming.md | Official API docs | 2026-07-03 |
| S3 | Anthropic — Effective context engineering for AI agents | https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents | Official engineering guidance | 2026-07-03 |
| S4 | LangChain — Syncing data sources to vector stores (+ `index()` API ref) | https://www.langchain.com/blog/syncing-data-sources-to-vector-stores | Framework maintainer | 2026-07-01 (RAG spec) |
| S5 | LlamaIndex — Ingestion Pipeline & Document Management | https://developers.llamaindex.ai/python/framework/module_guides/loading/ingestion_pipeline/ | Framework maintainer | 2026-07-01 (RAG spec) |

S4/S5 were scraped and quoted during the RAG spec work (see `docs/superpowers/specs/2026-07-01-scraping-rag-pipeline-design.md` §Evidence); not re-scraped here.

---

## 2. Caching plan — claim-by-claim evidence (S1, S2)

| # | Claim in plan | Evidence (S1 unless noted) | Status |
|---|---|---|---|
| C1 | `cache_control: {"type": "ephemeral"}` on a content block; last-block marker caches everything before it (hierarchy tools → system → messages) | Invalidation-hierarchy table; "Changes at `system` level invalidate `system` and `messages` caches but leave `tools` cache intact" | ✅ |
| C2 | Minimum cacheable prefix for `claude-opus-4-8` is **1,024 tokens**; shorter prompts fail silently | "1,024 tokens for Claude Opus 4.8, Claude Sonnet 5, …"; "The system will process them without caching and return no error" | ✅ |
| C3 | Writes 1.25× (5-min TTL), reads 0.1× | Pricing table: 5-minute writes 1.25× base; reads 0.1× ($0.50/MTok on $5 base) | ✅ |
| C4 | **Write premium applies to newly written tokens only**, not the whole prefix (Task 4's incremental economics) | "Only the new blocks get the 1.25x or 2x multiplier — the previously cached content being read uses the 0.1x refresh rate" | ✅ (plan wording corrected — see D1) |
| C5 | Reading refreshes the 5-min TTL at no extra cost | "The cache is refreshed for no additional cost each time the cached content is used" | ✅ |
| C6 | Moving-breakpoint multi-turn pattern: mark the last block each request; lookback finds the prior entry; old marker positions need no cleanup | "The cache breakpoint automatically moves to the last cacheable block in each request, so you don't need to update any `cache_control` markers as the conversation grows"; "it walks backward one block at a time, checking whether the prefix hash at each earlier position matches something already in the cache" | ✅ — Task 4's per-call marker-on-copy is exactly this pattern |
| C7 | 20-block lookback window (basis for the 10+-parallel-tool-calls caveat) | "the system checks at most 20 positions per breakpoint (counting the breakpoint itself as the first)" | ✅ |
| C8 | Max 4 breakpoints per request (we use 3) | "You can define up to 4 cache breakpoints" | ✅ |
| C9 | `tool_result` and image blocks may carry `cache_control` (Task 4 marks whatever block is last — text, tool_result, or image) | "Tool use and tool results: Content blocks in `messages.content` array (both user and assistant)" can be marked; "Images & Documents … (user turns only)" | ✅ — images only appear in user turns in ClaudIA (screenshot attach), so the user-turn restriction is satisfied |
| C10 | Empty text blocks must not be marked (Task 4's empty-string guard) | "Empty text blocks cannot be cached" | ✅ |
| C11 | Tool-definition changes invalidate everything (TV-bridge swap, new RAG tools → expected one-time invalidation) | Hierarchy table: tool definitions ✘/✘/✘ across all three caches | ✅ |
| C12 | Streaming compatible; usage arrives on `message_start` (Task 3 reads `event.message.usage`) | S1: usage reported in `message_start`; S2: `message_start` "contains a `Message` object" with `usage` (e.g. `"usage": {"input_tokens": 25, "output_tokens": 1}`) — cache fields appear in `usage` when caching is active. Task 3's `getattr(..., None) or 0` tolerates absent fields. | ✅ |
| C13 | Mid-conversation `role:"system"` messages preserve the cached prefix on Opus 4.8 (the docs-offered alternative to editing top-level `system` mid-session) | S1 (re-scraped 2026-07-03): "On Claude Opus 4.8, you can add a new system instruction partway through a conversation without invalidating the system or message caches. Append a `{\"role\": \"system\"}` message to `messages` instead of editing the top-level `system` field, so the cached prefix stays unchanged." | ✅ — evaluated and **rejected** for principles hot-reload (a rules replacement must not leave stale principles authoritative in the cached prefix; versioning keys off top-level content); decision + rationale recorded in the plan §Context & principles integration |

---

## 3. Architecture-review design rules — evidence (S3, S4, S5)

| # | Rule in review | Evidence | Status |
|---|---|---|---|
| R1 | Retrieval must stay **tool-shaped** (just-in-time), never pre-stuffed into the system prompt | S3: "agents built with the 'just-in-time' approach maintain lightweight identifiers … and use these references to dynamically load data into context at runtime using tools" | ✅ |
| R1b | System prompt stays minimal and stable; dynamic data enters via tools/messages | S3: strive "for the minimal set of information that fully outlines your expected behavior"; dynamic data enters through tools and retrieval at runtime, not the static system prompt. Consistent with ClaudIA hard rule 4 and with C1/C11 cache economics. | ✅ |
| R4 | Retrievals are per-turn; stale tool results need not persist in context | S3: "tool result clearing" — "once a tool executes deep in history, the raw result becomes superfluous … one of the safest lightest touch forms of compaction." ClaudIA's skip-tool-rows-on-rebuild (M1) is a coarse version of the same principle. | ✅ |
| R5 | Memory outside the context window (notes/archive) pulled back on demand | S3: agents "regularly write notes persisted to memory outside of the context window. These notes get pulled back into the context window at later times" — matches claudia.db + `search_past_conversations` (on-demand recall, not auto-injection) | ✅ |
| R6 | Layer-2/3 contract (doc_id, source_url, content_hash SHA-256, saved_at; enumerate-all; read-by-id) | S4: RecordManager stores "the document hash …, write time, the source id"; SHA-1 collision warning → SHA-256. S5: docstore `doc_id → document_hash` dedup. Quoted in the RAG spec §Evidence. | ✅ (verified 2026-07-01) |

---

## 4. Consistency / logic-defect pass — results

Run 2026-07-03 against the scraped evidence and the code. **Two defects found and fixed; four items double-checked clean; two new caveats surfaced and documented.**

### Defects found → fixed in the plan

| ID | Defect | Fix |
|---|---|---|
| D1 | Plan said each tool-loop turn "pays **full price** only for the blocks added since the previous turn." Wrong rate: newly appended blocks are *written to cache* at **1.25×** (then read at 0.1× next call) — see C4. Economically still correct to cache (1.25× once + 0.1× thereafter beats 1.0× every call whenever ≥1 follow-up call exists, which a tool loop guarantees), but the stated rate was inaccurate. | Task 4 intro + `_with_history_cache_marker` docstring rewritten with the correct 1.25×/0.1× split |
| D2 | Empty-string guard comment said "empty text block would be rejected by the API" — informal claim. The documented fact is C10: "empty text blocks cannot be cached." | Comment now cites the documented wording |

### New caveats surfaced by the pass → documented in the plan (gap-3 caveat list + Task 5 note text)

| ID | Caveat | Impact |
|---|---|---|
| N1 | **History-window slide.** `get_history(limit=40)` is a sliding window; once a session exceeds 40 rows, each user turn drops the oldest row, shifting every byte of the messages section → messages-cache miss once per user turn thereafter. Tools+system (the dominant prefix) stay cached; within-turn tool loops still cache fully. Bounded cost: ≤40 rows of text re-read once per turn. | Documented; optional hysteresis eviction (drop rows in blocks of ~10) noted as follow-up **if** the Task 3 usage logs show it matters. Not added to scope now — measure first. |
| N2 | **Cross-turn shape divergence.** The live tool-loop array holds block-form assistant text + tool_use/tool_result messages; the history rebuilt at the next user turn is plain strings with tool rows skipped. A new user turn therefore resumes from the cache entry written at the end of the *reconstructed* history (the previous turn's first request), not from mid-tool-loop entries. Expected behavior, now stated explicitly; Task 5 live-verification step 3 observes it. | Documented |

### Double-checked clean (no change needed)

1. **Marker-movement legality** — Task 4 rebuilds the marker on a fresh copy each call and never leaves stale markers; per C6 old positions need no cleanup and lookback bridges the gap. The implementation is the documented pattern exactly.
2. **Markable block types** — the last block at ClaudIA's breakpoint is always a text, tool_result, or user-turn image block; all three are markable per C9.
3. **Breakpoint budget** — 3 of 4 used; no path adds a fourth (C8).
4. **Mutation safety** — `_with_cache_marker` and `_with_history_cache_marker` copy the containers they modify (`list(...)` + `{**block, ...}`); module-level `_LOCAL_TOOLS` / `TOOL_DEFINITIONS` and the loop's working `messages` list are never touched. Regression tests assert this (Tasks 1 & 4).

### Round 2 — context/principles integration review (2026-07-03, same day)

Requested follow-up: verify the context.md/principles.md pipeline is fully integrated into the plan and matches documented best practices. Result — **integrated and clean**; two additions made to the plan:

1. **Pipeline stability table added** to the plan (§Context & principles integration): every stage checked for byte-stability per message — Drive override (session-fixed), local-file re-read (stable unless edited, `.strip()` consistent on both paths), version note (stable even under concurrent sessions — append-only `doc_versions` registry ordered ASC means a parallel session registering v(n+1) cannot change this session's "previous:" line), market-calendar block (date-keyed; byte-identical across same-day sessions, so a tab reload within TTL re-reads the cache), safety block (constant), past versions via `get_doc_version` tool (just-in-time retrieval per S3 — never in the system prompt).
2. **Two docs-offered alternatives evaluated and rejected with recorded rationale** (so they aren't re-litigated later): mid-conversation `role:"system"` injection on hot-reload (C13 — wrong tool for a rules *replacement*; right tool for future additive operator instructions) and multi-block system with separate breakpoints (only pays off on rare reloads; burns the 4th breakpoint; YAGNI).

No defects found in this round — the plan's Task 2 single-block design was already the fewest-breakpoints shape matching the actual stability boundary.

### Round 3 — load-time version checks (2026-07-03, user design decision)

The Round-2 stability table proved the per-message system-prompt rebuild produced *identical bytes* — but the user correctly flagged the deeper issue: producing identical bytes on every prompt is itself the defect. `handle_message` was doing **2 file reads + 1 `doc_versions` DB query per prompt** to rebuild an unchanged string. Doc versions and documents change only periodically and never without the operator knowing; checking them per prompt is overcheck that slows the agent.

**Decision (user, 2026-07-03):** version and document checks happen when ClaudIA loads, never per prompt. **Plan change:** new Task 3 — system prompt built once per session, cached on the agent, rebuilt only when the watchdog's `ContextLoader.reload_count` changes (event-driven; steady-state per-prompt cost = one int comparison). Hot-reload UX unchanged ("applies from your next message"); version registration was already load-time-only (`on_chat_start`). Tasks renumbered (logging → 4, history → 5, live verification → 6).

Best-practice alignment: S3's context-engineering guidance favors a stable, minimal system prompt assembled once with dynamic data entering via tools at runtime — a once-per-session build is the natural implementation of that; per-prompt reassembly was an artifact, not a requirement. Cache-wise the change is neutral-to-positive: byte-identity of the system segment is now guaranteed structurally rather than empirically.

### Verdict

The plan is consistent with the official documentation on every claim (C1–C12), the architecture-review rules are backed by Anthropic's own engineering guidance (R1–R5) and the framework-maintainer sources already verified in the RAG spec (R6). The two defects were wording-level (economics stated at the wrong rate; an informal justification), not design-level — the implementation code in the plan was already correct. Clear to implement.

# Architecture Review — agent.py Information Handling

**Date:** 2026-07-03
**Scope:** How `claudia/agent.py` acquires, injects, persists, and retrieves information, across four areas: (1) prompts, (2) session memory archive, (3) access to scrapes, (4) knowledge / RAG (not yet deployed). Includes the requested correctness review of the GDrive save-at-stop / reload-at-start flow for `claudia.db`.
**Purpose:** Establish the complete information-handling map so the RAG plan (`docs/superpowers/specs/2026-07-01-scraping-rag-pipeline-design.md`) can be implemented against verified reality, not assumptions.
**Method:** Code read of `agent.py`, `context_loader.py`, `conversation_store.py`, `gdrive_sync.py`, `app.py`, `session_reporter.py`, and `ibkr_core_mcp/claude_tools.py`; official-docs verification where API behavior is asserted.
**Source evidence:** every API-behavior and best-practice claim in this review and in the caching plan is backed by scraped authoritative sources — see `docs/2026-07-03-llm-best-practices-sources.md` (Anthropic prompt-caching & streaming docs, Anthropic context-engineering guidance, LangChain/LlamaIndex).

---

## 1. Information flow map

```
                         ┌────────────────────────── SYSTEM PROMPT (per message) ─┐
 Drive context.md ──┐    │ version note → context.md → principles.md              │
 local context.md ──┼──► │ → market calendar (per session) → _SAFETY_BLOCK (fixed)│
 Drive principles ──┤    └──────────────────────────────────────────────────────┬─┘
 local principles ──┘         ▲ hot-reload (watchdog)                            │
                              ▲ doc_versions (SHA-256, v1/v2…)                   │
                                                                                 ▼
 claudia.db ── get_history(session_id, 40) ── _history_to_messages ──► MESSAGES ──► client.messages.stream()
   ▲   ▲          (tool rows skipped)              + images                 ▲            tools = toolkit.tools (42)
   │   │                                                                    │                  + TV extra_tools (0–16)
   │   └── add_message() every user / assistant / tool turn ◄──────────────┘                  + _LOCAL_TOOLS (4)
   │
   ├── FTS5 (messages.content only) ◄── search_past_conversations tool
   ├── decisions (+FTS, unused), relationships (unused), doc_versions
   ├── session report → data/test-sessions/YYYY-MM-DD-HHmm.md
   └── GDrive db/claudia.db  (download: first session per process; upload: session end)

 Web:  firecrawl_search/crawl (toolkit) ──► Drive web_docs/   [WRITE-ONLY today]
       fetch_web_page (local tool)      ──► conversation only [never persisted to web_docs]

 Knowledge (future): web_docs/ ── indexer ──► knowledge.db ── search_knowledge_base tool
```

---

## 2. Area 1 — Prompts

### How it works (verified)

| Stage | Mechanism | File:line |
|---|---|---|
| Source load | Drive `context.md`/`principles.md` fetched **every session start**; overrides local `docs/` files. Fallback to local on any Drive error. 1 MB size guard. | app.py:432, gdrive_sync.py:275 |
| Hot-reload | Watchdog on local files; an edit clears the Drive override and applies from the next message. Chat notification via `call_soon_threadsafe` + captured contextvars. | context_loader.py:100, app.py:455 |
| Versioning | SHA-256 of stripped context+principles → `doc_versions` (v1, v2…); human snapshot to `docs/versions/{label}/`; hash-change WARNING in chat; version label injected as the first line of the system prompt. | app.py:474–494, conversation_store.py:178 |
| Assembly | `_build_system_prompt` = version note + context + principles + market-calendar block (per session, from `SQLiteStore.get_market_calendar_context`) + `_SAFETY_BLOCK`. | agent.py:219 |
| Trust boundary | `_SAFETY_BLOCK` is a hardcoded module constant, appended **last and unconditionally** — user-editable docs cannot suppress it. Conversation history is injected as `role: user/assistant` messages, never into the system prompt (hard rule 4). | agent.py:46, 231 |

### Assessment

**Sound.** Clear precedence (Drive > local), integrity tracking (versions + hash alerts), and a correct trust boundary (safety block outside user-editable content, history outside the system prompt — the prompt-injection-safe layout).

Prompt-cache interplay (relevant to the in-flight caching upgrade, plan `docs/superpowers/plans/2026-07-03-prompt-caching-upgrade.md`): the system prompt is byte-stable within a session — no timestamps, UUIDs, or per-request interpolation anywhere in the pipeline. The only invalidators are hot-reload and version change, both intentional and rare.

One structural note, no action needed: the version note sits at the *front* of the system prompt, so a version bump invalidates the entire system+messages cache. That is semantically correct — a rules change *should* be a hard boundary.

---

## 3. Area 2 — Session memory archive

### Write path (verified)

Every user message, assistant response, and tool call is persisted to `claudia.db → messages` (`add_message`, conversation_store.py:242). Tool rows store `tool_name`, `tool_input_json`, `tool_result_json` with `content=""`. Order proposals additionally write to `decisions` tagged with `doc_version`. Session close writes `ended_at` + metadata and a Markdown session report to `data/test-sessions/`.

### Read paths (verified)

1. **Context injection** — `get_history(session_id, limit=40)` returns the *current session only*, newest-40 rows re-sorted ascending. `_history_to_messages` (agent.py:234) converts user/assistant rows and **skips tool rows** — documented and correct: `tool_use_id`s are not persisted, and orphaned `tool_result` blocks cause API 400s.
2. **Cross-session recall** — `search_past_conversations` tool → FTS5 over `messages.content`, 5 results, ~300-char snippets with dates.

### Important semantic clarification (for the "saved at end / re-loaded at start" design intent)

The **archive** round-trips through Drive correctly, but a new session does **not** re-load prior conversation into the model's context. Each Chainlit tab gets a fresh `session_id`; `get_history` is scoped to it, so the model starts with an empty message list. Continuity across sessions is **on-demand** via the FTS search tool, not automatic. This is a deliberate design (bounded context, rule 4), but it should be understood as: *the database is restored; the conversation context is not.*

### GDrive save/reload correctness review (requested)

**What is implemented well:**
- Download: first session per process, **before** the store opens; streamed to a temp file; `PRAGMA integrity_check` before replacing local; failed/corrupt download never touches the local copy (gdrive_sync.py:178–238).
- Upload: after `close_session`, via `files().update()` in-place (preserves file identity/sharing); create-vs-update race guarded by an `RLock`; all failures non-fatal with the local copy preserved (gdrive_sync.py:240–271).
- `trashed=false` in every Drive query; token refresh with 0o600 enforcement; `ping()` gives real API reachability to the status lights.

**Findings — three gaps, ordered by severity. ✅ ALL THREE FIXED 2026-07-03** (commits 4c0edd6 G1, 7e65d9b G2, d39d52b G3 — TDD, 17 gdrive tests green; CLAUDE.md corrected):

| ID | Severity | Finding |
|---|---|---|
| **G1** | **Medium** | **No WAL checkpoint before upload — CLAUDE.md's claim is not implemented.** `grep -rn wal_checkpoint claudia/` is empty. The DB runs in WAL mode (conversation_store.py:46), and `upload_db` streams the raw `.db` file, which excludes un-checkpointed frames in `claudia.db-wal`. Today this *usually* works only because `_conn()` opens a connection per operation and SQLite auto-checkpoints when the last connection closes. That is not a guarantee: a second browser tab mid-operation at upload time can prevent or truncate the checkpoint, and a checkpoint racing `MediaFileUpload`'s sequential read can produce a torn upload. **Fix:** immediately before upload, copy via the SQLite backup API (`sqlite3.connect(db).backup(temp)`) and upload the temp file — atomic, WAL-safe, and read-consistent. (An explicit `PRAGMA wal_checkpoint(TRUNCATE)` is the minimal alternative but does not close the torn-read window.) Also correct CLAUDE.md, which currently documents a checkpoint that does not exist. |
| **G2** | **Medium** | **Stale-Drive overwrite after a failed upload.** If the end-session upload fails (network down) and the process then restarts, `download_db` runs (first-session gate, app.py:423) and **replaces the newer local DB with the older Drive copy** — silently losing the last session's messages. CLAUDE.md's "local copy preserved; syncs next session" only holds if the process does *not* restart in between. **Fix:** freshness guard — fetch the Drive file's `modifiedTime` and skip the download when the local file is newer (or download to a side path and merge-decide). |
| **G3** | Low | **Stale `-wal`/`-shm` sidecars after a crash.** `shutil.move` replaces `claudia.db` but leaves any `claudia.db-wal`/`-shm` from a crashed prior run; SQLite will attempt to recover those old WAL frames into the *newly downloaded* file on first open. Narrow window (crash + restart + Drive copy present) but corrupting when hit. **Fix:** unlink both sidecars when replacing the DB in `download_db`. |

**Additional archive findings (not GDrive):**

| ID | Severity | Finding |
|---|---|---|
| **M1** | Medium (RAG-relevant) | **Tool results are persisted but unreachable.** Tool rows carry `content=""`; the FTS index covers `messages.content` only — so `tool_result_json` (every market-data pull, every scrape that entered the conversation) is stored but not searchable, and tool rows are also skipped from history reconstruction. Net: tool output survives only as the assistant's prose summary. Acceptable today; becomes a real gap if the RAG plan assumes past tool data is recoverable — it is not. |
| **M2** | Low | **Dead schema:** `relationships` (add/get, conversation_store.py:400) and `decisions_fts`/`search_decisions` (line 371) have **no callers and no tool** — "accumulated symbol-level observations" (CLAUDE.md) is aspirational, not wired. Either wire them (a `record_observation`/`recall_symbol` tool pair) or remove — per the clean-architecture preference, unused capability is a liability. |
| **M3** | Info | History reconstruction drops images (only text persisted) and `_HISTORY_LIMIT=40` counts *rows* including skipped tool rows, so tool-heavy sessions carry fewer real turns of context than 40. Both fine, just document. |

---

## 4. Area 3 — Access to scrapes

### Ingestion surfaces (verified)

| Surface | Where | Persistence | Guard |
|---|---|---|---|
| `firecrawl_search` | toolkit (ibkr_core_mcp) | optional snapshot → Drive `web_docs/searches/` | SSRF-guarded in web_scraper; keyless-tier fallback |
| `firecrawl_crawl` | toolkit | pages + `index.json` → Drive `web_docs/{site}/` | two-layer SSRF guard; Crawl4AI Playwright fallback on low-quality content |
| `fetch_web_page` | `_LOCAL_TOOLS` (agent.py:496) | **none** — result exists only in the conversation | own SSRF guard: scheme allowlist, localhost/link-local/private rejection, DNS re-resolution of hostnames; 15 s timeout; 12,000-char cap |

### Assessment

**The retrieval story is the known flaw, and it is worse than "write-only".** Two independent blind spots compound:

1. **Drive store is write-only** (the layer-2 problem the RAG spec addresses): no list/read/delete tools, so ClaudIA must re-scrape content it already saved.
2. **The conversation archive doesn't cover it either** (finding M1): scraped markdown that entered the conversation is stored as `tool_result_json`, which is neither FTS-indexed nor replayed into later context. So today there is **no path whatsoever** by which ClaudIA can retrieve previously scraped content — not by Drive, not by memory search. Layer 2 (list/read/delete) fixes the first; layers 3/4 fix the semantic side.

Also note the asymmetry: `fetch_web_page` results bypass `web_docs/` entirely. After layer 2 ships, consider either routing `fetch_web_page` saves through the same store or explicitly documenting it as ephemeral-by-design.

**Security residual (S1, low, verify):** `_fetch_web_page` uses `allow_redirects=True`; the SSRF guard checks the *initial* URL only, so a public URL that 302-redirects to a private address would be followed by `requests` without re-validation (classic redirect-SSRF). The ibkr_core_mcp scraper had its SSRF pass audited (2026-06-25, 20 regression tests) — verify whether redirect handling was in that scope for the local tool too; if not, disable redirects or re-validate each hop.

---

## 5. Area 4 — Knowledge / RAG (not yet deployed)

### Current state

Nothing deployed. The four-layer design is approved (spec 2026-07-01): layer 1 ingestion (shipped) → layer 2 document-store CRUD over `web_docs/` (**build next**) → layer 3 `knowledge.db` index (sqlite-vec + sentence-transformers) → layer 4 `search_knowledge_base` retrieval tool. The layer-2/3 contract (`doc_id`, `source_url`, `content_hash` SHA-256, `saved_at`, enumerate-all + read-by-id) matches the LangChain RecordManager / LlamaIndex docstore pattern and is sound.

### How RAG plugs into agent.py (verified — zero claudia_ui changes)

New tools land in `TOOL_SCHEMAS` + handler dispatch in `ibkr_core_mcp/claude_tools.py`, flow through `ClaudeToolkit.tools` → `_all_tools` (agent.py:296) automatically — same path as `firecrawl_search`. Retrieval results arrive as `tool_result` blocks in the messages array.

### Design constraints the RAG implementation MUST respect (from this review)

1. **Retrieval-as-tool is the cache-correct shape.** With the prompt-caching upgrade in place (3 breakpoints: tools → system → messages), retrieved chunks arrive *after* the cached prefix — every retrieval is cache-friendly. The anti-pattern to reject explicitly: injecting retrieved documents into the **system prompt**. That would invalidate the system+messages cache on every turn *and* violate hard rule 4 (nothing dynamic in the system prompt).
2. **One-time cache invalidation per deploy is expected.** Adding `list_web_docs`/`read_web_doc`/`delete_web_docs`/`search_knowledge_base` changes the tools array → full cache rebuild on first call after deploy. Harmless; the `prompt cache:` log line (Task 3 of the caching plan) makes it visible.
3. **Data-integrity rule alignment.** The `_SAFETY_BLOCK` requires every stated fact to come from a tool result or the user. RAG chunks arrive as tool results, so they satisfy the rule — **provided** `search_knowledge_base` returns `source_url` + `doc_id` with each chunk so ClaudIA can attribute, and `read_web_doc(doc_id)` provides the full-document citation path. The spec already defines both; treat them as non-negotiable at implementation.
4. **Retrievals are per-turn, not persistent context.** Because tool rows are skipped from history reconstruction (M1), a chunk retrieved in turn N is *gone* from the model's context in turn N+1 (only the assistant's summary survives). For RAG this is actually the right default — re-query rather than carry stale chunks — but the implementer must know it: do **not** design flows that assume a previously retrieved chunk is still visible.
5. **Storage boundaries stay hard.** Three databases, one owner each: `claudia.db` (conversation), `~/.ibkr_core/store.db` (trades/market data), `knowledge.db` (vectors — new). The spec's rule that layer 2 holds the only master copy and `knowledge.db` holds pointers prevents a fourth shadow store. Retrieved chunk text will additionally accumulate in `messages.tool_result_json` (per M1, inert) — acceptable, but worth a size check after the first month of use.
6. **Indexing runs out-of-band.** The chat loop (`handle_message`) must never embed/index inline — the indexer (layer 3) is a separate job over `list_web_docs`, per the spec's enumerate→diff→upsert cycle. The partial-enumeration → wrongful-deletion risk in the spec is the one to test hardest (data integrity non-negotiable).

### Readiness verdict

The layer-2 contract is implementable against the current code with no surprises; agent.py needs zero changes for layers 2 and 4 (tools flow in automatically). The two prerequisites this review adds to the RAG plan: fix **M1-awareness** in the design (don't assume archived tool data is retrievable) and land the **prompt-caching upgrade first** so retrieval traffic is priced at cache-read rates from day one.

---

## 6. Consolidated findings and recommendations

| # | Area | Severity | Finding | Recommendation |
|---|---|---|---|---|
| G1 | GDrive sync | ~~Medium~~ **FIXED** | No WAL checkpoint before `upload_db`; CLAUDE.md documented one that didn't exist; torn/stale upload possible | ✅ 2026-07-03 (4c0edd6): `sqlite3.Connection.backup()` snapshot uploaded instead of the live file; CLAUDE.md corrected |
| G2 | GDrive sync | ~~Medium~~ **FIXED** | Failed upload + process restart → older Drive copy overwrites newer local DB (silent session loss) | ✅ 2026-07-03 (7e65d9b): freshness guard — Drive `modifiedTime` vs local mtime (incl. `-wal`); local-newer keeps local |
| G3 | GDrive sync | ~~Low~~ **FIXED** | Stale `-wal`/`-shm` sidecars can be replayed into a freshly downloaded DB after a crash | ✅ 2026-07-03 (d39d52b): both sidecars unlinked before the downloaded file lands |
| M1 | Memory | **Accepted** (user, 2026-07-03) | Tool results persisted but neither FTS-indexed nor replayed — no retrieval path for past tool/scrape data | Left as-is until RAG is implemented; RAG design must not assume archived tool data is recoverable |
| M2 | Memory | ~~Low~~ **FIXED** | `relationships` table and `search_decisions` were dead code (no tool, no caller) | ✅ 2026-07-03 (ddb0ef9): removed — data-safe migration (derived FTS dropped; `relationships` dropped only if provably empty); symbol knowledge belongs to the planned knowledge layer |
| S1 | Scrapes | ~~Low (verify)~~ **CONFIRMED + FIXED** | `fetch_web_page` followed redirects without re-running the SSRF guard per hop — verified outside the 2026-06-25 audit's scope (H-1 covered initial-URL only; residual listed was DNS rebinding, not redirects) | ✅ 2026-07-03 (1ea122d): manual redirect loop (max 5 hops), full guard re-run per hop; SECURITY.md §8 + checklist updated |
| R1 | RAG | Design rule | Retrieval must stay tool-shaped; never inject retrieved docs into the system prompt | Encode in the layer-4 implementation plan |
| R2 | RAG | Sequencing | Prompt-caching upgrade should land before RAG so retrieval turns read the prefix at 0.1× | Execute `docs/superpowers/plans/2026-07-03-prompt-caching-upgrade.md` first |

**What is verified healthy:** prompt assembly and trust boundary; doc versioning; per-op WAL connections; download integrity-check-before-replace; upload race lock; FTS search for prose history; SSRF guards on all three web surfaces (modulo S1); the layer-2/3 RAG contract; agent.py's tool merge path for future RAG tools.

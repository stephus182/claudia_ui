# Scraping and RAG — Layers in One Pipeline

**Status:** Design approved 2026-07-01. Layer 2 to be implemented next; Layer 3/4 (RAG) is spec-only, built after v1.0 live tests.

## Problem

`ibkr_core_mcp` can scrape the web (`firecrawl_search`, `firecrawl_crawl`) and **store** results to Google Drive under `web_docs/`. But the store is **write-only**: there is no tool to list, read, or delete previously-scraped docs. ClaudIA must re-scrape every time it needs content it already saved. Storing docs you cannot retrieve is wasted work.

Separately, a vector RAG knowledge base is planned ([[future-knowledge-base]]: `sqlite-vec` + `sentence-transformers`, separate `knowledge.db`). The open question was whether "read scraped docs" and "the knowledge base" are two efforts or one. **They are one pipeline in layers** — this spec defines the boundary so neither duplicates the other.

## Architecture — four layers, each owns one thing

| Layer | Owns | Component | Status |
|---|---|---|---|
| 1. Ingestion | Fetching raw content | `firecrawl_search` / `firecrawl_crawl` | Shipped |
| 2. Document store | Raw markdown + metadata; CRUD + lifecycle | Drive `web_docs/` + **new** list/read/delete tools | **Build now** |
| 3. Index | Chunks + embeddings + pointers back to layer 2 | `knowledge.db` (`sqlite-vec`) | Spec only |
| 4. Retrieval | Semantic/hybrid search → chunks + citations | `search_knowledge_base` tool | Spec only |

**The single rule that prevents duplication:** the document store (layer 2) holds the *only* master copy of the raw bytes. The vector index (layer 3) stores embeddings + chunk text + a *pointer* (`doc_id`, `source_url`, `content_hash`) back to layer 2 — never a second master. The KB **indexes** the store and **cites** back into it; it does not re-store documents.

### Evidence — this is the canonical shape

Both leading RAG frameworks implement exactly this separation:

- **LangChain Indexing API** uses a `RecordManager` (a SQLite table) kept *separate* from the vector store. It stores, per document: **"the document hash (hash of both page content and metadata), write time, the source id."** That is precisely the layer-2 → layer-3 contract below. Source: https://www.langchain.com/blog/syncing-data-sources-to-vector-stores and API reference https://reference.langchain.com/python/langchain-core/indexing/api/index
- **LlamaIndex Ingestion Pipeline** attaches a `docstore` holding a `doc_id → document_hash` map, separate from the vector DB: hash changed → re-embed and upsert; hash unchanged → skip. Source: https://developers.llamaindex.ai/python/framework/module_guides/loading/ingestion_pipeline/

## The integration contract (the seam that makes it "zero rework")

Every doc the store exposes carries a stable, hashable identity. This is the *entire* interface the future KB consumes:

| Field | Meaning | Notes |
|---|---|---|
| `doc_id` | Stable unique id | Drive `file_id` (stable across reads) |
| `source_url` | Origin (page URL, or search query) | LangChain's "source id" |
| `content_hash` | SHA-256 of `markdown + source_url` | Change detection; **SHA-256, not SHA-1** (LangChain warns SHA-1 is not collision-resistant) |
| `saved_at` | ISO-8601 UTC timestamp | LangChain's "write time"; freshness/TTL |

Plus two access guarantees (metadata alone is insufficient — confirmed by the research):

- **Enumerate-all** — `list_web_docs` must be able to return the *complete* set of stored docs. LangChain's `full` cleanup mode requires the loader to return the entire dataset; a partial listing would cause wrongful deletion (see Risks).
- **Read-full-by-id** — `read_web_doc(doc_id)` returns raw markdown to chunk + embed.

Deletion sync (layer 3, later) is then just: re-enumerate → diff `doc_id`s → drop vectors for vanished ids → re-embed where `content_hash` changed. This is LangChain's `full` / `scoped_full` cleanup, and LlamaIndex's docstore dedup, expressed over our store.

---

## Layer 2 — document-store CRUD (BUILD NOW)

New tools in `ibkr_core_mcp`, added to `TOOL_SCHEMAS` + handler dispatch in `ibkr_core_mcp/claude_tools.py`. They flow into ClaudIA automatically via `ClaudeToolkit.tools` at `claudia/agent.py:281` — no claudia_ui change needed, same pattern as `firecrawl_search`. **No semantic search, no embeddings, no ranking** — that boundary is what keeps this from becoming the KB.

### Tools

```
list_web_docs(kind=None, url_contains=None) -> list[dict]
  Enumerate every stored doc. Optional metadata-only filters:
    kind          "crawl_page" | "search_snapshot" (default: both)
    url_contains  substring match on source_url/query (metadata, NOT content)
  Returns per doc: {doc_id, kind, source_url, title, content_hash,
                    saved_at, site, stale}
  MUST return the complete set (see Risk: partial enumeration).

read_web_doc(doc_id) -> dict
  Fetch one doc's full markdown by doc_id.
  Returns {doc_id, source_url, title, saved_at, content_hash, markdown}

delete_web_docs(doc_id=None, site=None, older_than_days=None) -> dict
  Remove docs by one selector:
    doc_id           delete a single doc
    site             delete a whole crawled site (folder + manifest)
    older_than_days  prune snapshots older than N days (obsolete cleanup)
  Returns {deleted_count, deleted_ids}
```

Three tools = complete CRUD, since **create** already exists (`firecrawl_*`). Kept minimal per the clean-architecture preference ([[feedback-clean-architecture]]).

### Write-path change (required — honestly, not free)

Today `index.json` is `{url, crawled_at, pages:[{url, file_id}]}` — **no hash, no title**, and `searches/` files have **no manifest at all**. To satisfy the contract without `list_web_docs` having to download every file to hash it, the store must persist metadata **at save time**:

- `save_crawl` — add `content_hash` (SHA-256 of markdown + url) and `title` per page in `index.json`.
- `save_search` — write a parallel manifest so search snapshots are first-class docs with the same fields.

This touches `WebDocsStore.save_crawl` / `save_search` in `ibkr_core_mcp/web_scraper.py`. It is additive and backward-compatible (missing hash → compute lazily on first read, then backfill). "Zero rework" applies to the *future KB*, not to this write-path addition now.

### `stale` flag

`list_web_docs` returns `stale=True` when `saved_at` is older than a configurable freshness window (default 30 days). Advisory only — informs ClaudIA and the future prune job; does not auto-delete.

### Security

Read/list/delete operate **only** on Drive `web_docs/` via existing OAuth credentials. No URL fetching → **no SSRF surface** (unlike `firecrawl_crawl`). No new env vars. `delete_web_docs` is scoped to the `web_docs/` subtree — it cannot touch `db/`, `market_data/`, or `account_data/`.

### Testing (layer 2)

- Unit: manifest round-trip (save → list → read → delete), hash stability, `older_than_days` pruning boundary, empty-store behavior, filter correctness.
- Integration (Drive): full lifecycle against a scratch `web_docs/` folder; verify `delete_web_docs(site=...)` removes folder + manifest; verify partial-enumeration guard (below).

---

## Layer 3 / 4 — RAG knowledge base (SPEC ONLY — build after v1.0 live tests)

Per [[future-knowledge-base]]: separate `knowledge.db`, never mixed with `claudia.db` or `store.db`.

### Indexer (layer 3)

```
1. enumerate  = list_web_docs()            # complete listing (guarded)
2. for each doc:
     if doc_id new OR content_hash changed:
        markdown = read_web_doc(doc_id)     # layer-2 read
        chunks   = split(markdown)          # SentenceSplitter-style
        vectors  = embed(chunks)            # sentence-transformers
        upsert(knowledge.db, {vector, chunk_text, doc_id, source_url, content_hash})
     else: skip
3. deletion sync: drop vectors whose doc_id is absent from `enumerate`
```

### Build-vs-reuse decision (deferred, for the "no double effort" goal)

Evaluate at implementation time — do not hand-roll a sync engine before checking reuse:

- **Option A — adopt LangChain `index()` + `SQLRecordManager`** pointed at a `sqlite-vec`-backed vector store. The RecordManager already implements hash tracking, write-time, source-id, and `incremental`/`full`/`scoped_full` cleanup. Could eliminate most of the layer-3 sync code. Cost: a LangChain dependency.
- **Option B — hand-roll** the `doc_id → content_hash` map over `sqlite-vec` directly (LlamaIndex docstore pattern). Fewer deps, more code to own and test.

The layer-2 contract is deliberately shaped so **either** option consumes it with zero changes.

### Retrieval (layer 4)

```
search_knowledge_base(query, k=5) -> list[dict]
  Hybrid: vector similarity (+ optional metadata filter) over knowledge.db.
  Returns chunks with {chunk_text, doc_id, source_url, score}.
  ClaudIA cites a full doc via read_web_doc(doc_id) when the whole page is needed.
```

`search_knowledge_base` is the *only* semantic-search surface. `list_web_docs` stays metadata-only. That line is the anti-duplication boundary, in writing.

---

## Risks / errors surfaced by the research

1. **Partial enumeration → wrongful deletion.** LangChain's docs warn: in `full` cleanup mode, if the loader returns only a subset, it deletes documents it should not. For us: if layer-3 deletion-sync runs against an *incomplete* `list_web_docs` (Drive pagination fails mid-listing), it would evict live vectors. **Mitigation:** `list_web_docs` must return a provably-complete listing (paginate to exhaustion, raise on partial failure rather than returning a truncated set); deletion-sync only runs on a complete enumeration. Data integrity is non-negotiable ([[feedback-rigor-integrity]]).
2. **Hash algorithm.** Use SHA-256 over content **+ metadata** (source_url). SHA-1 (a common default) is not collision-resistant per LangChain's own warning.
3. **Backward compatibility.** Existing `web_docs/` folders predate `content_hash` in the manifest. `list_web_docs`/indexer must tolerate a missing hash: compute on first read, backfill the manifest.
4. **Scope creep into the KB.** Any temptation to add keyword/semantic search to `list_web_docs` is the trap — it silently becomes the KB in the wrong layer. Keep layer 2 metadata-only.

## Out of scope (YAGNI)

- Embeddings, vector search, chunking — all layer 3/4, deferred.
- Cross-doc full-text search — belongs to the KB, not layer 2.
- Auto-deletion on staleness — `stale` is advisory; deletion is explicit via `delete_web_docs`.

## References

- LangChain — Syncing data sources to vector stores (RecordManager, cleanup modes): https://www.langchain.com/blog/syncing-data-sources-to-vector-stores
- LangChain — `index()` API reference (cleanup modes, key_encoder, SHA warning): https://reference.langchain.com/python/langchain-core/indexing/api/index
- LlamaIndex — Ingestion Pipeline & Document Management (docstore, doc_id→hash dedup): https://developers.llamaindex.ai/python/framework/module_guides/loading/ingestion_pipeline/
- Firecrawl (ingestion layer, already integrated): https://docs.firecrawl.dev
- Code touch points: `ibkr_core_mcp/web_scraper.py` (`WebDocsStore`), `ibkr_core_mcp/claude_tools.py` (`ClaudeToolkit`, `TOOL_SCHEMAS`), `claudia/agent.py:281` (tool merge).

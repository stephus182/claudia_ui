# Conversation Memory Reference

All interactions are stored in `data/claudia.db` (separate from ibkr_core_mcp's
`~/.ibkr_core/store.db`).

| Table | Contents |
|---|---|
| `sessions` | One row per Chainlit session, with start/end time, document hash, and `doc_version` |
| `messages` | Full message history (user, assistant, tool calls and results) — primary memory store |
| `decisions` | User-directed trade proposals surfaced by ClaudIA — each tagged with `doc_version`. ClaudIA does not decide to trade; it surfaces a proposal when directed by the user. The user decides at the button → Touch ID → confirmation dialog. |
| `doc_versions` | Versioned snapshots of `context.md` + `principles.md` — full text, hash, date |

(A `relationships` table and a decisions FTS index were removed 2026-07-03 — never wired to
any caller; symbol-level knowledge belongs to the planned knowledge layer. Existing DBs are
migrated safely: derived index dropped, `relationships` dropped only if empty.)

**Search:** ClaudIA uses SQLite FTS5 to search full conversation history. Ask: *"What did we
discuss about NVDA last month?"* The `search_past_conversations` tool searches all messages
across all sessions. Results include the doc version active at the time.

**Version snapshots** are also written to `docs/versions/{label}/` as human-readable files for
reference.

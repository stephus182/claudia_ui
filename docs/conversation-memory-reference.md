# Conversation Memory Reference

All interactions are stored in `data/claudia.db` (separate from ibkr_core_mcp's
`~/.ibkr_core/store.db`).

| Table | Contents |
|---|---|
| `sessions` | One row per Panel session, with start/end time, document hash, and `doc_version` |
| `messages` | Full message history (user, assistant, tool calls and results) — primary memory store. **On a `tool` row, `content` is an origin stamp, not text.** The model's own calls write `""`; a call made outside the agent loop writes who caused it — `ui_button` (a click) or `startup` (a background task), both from `claudia/tool_record.py`. The stamp exists because the forensic rule *a `tool` row between a user row and an assistant row is the model's evidence* becomes false the moment something other than the model can write one. Read by an audit, never by the model: `tool` rows are skipped by the history replay (they carry no Anthropic `tool_use_id`, so replaying them would 400) and excluded from recall. |
| `message_withdrawals` | One row per assistant message a claim detector contradicted (`message_id`, `withdrawn_at`, 2026-09-11). The replay skips such a row; the chat, the report and the store keep it. A side table, not a column: `messages` is append-only (asserted by `test_no_sql_in_the_package_updates_a_message_row`), and a withdrawal is an event about a row, not a change to it |
| `decisions` | User-directed trade proposals surfaced by ClaudIA — each tagged with `doc_version`. ClaudIA does not decide to trade; it surfaces a proposal when directed by the user. The user decides at the button → Touch ID → confirmation dialog. Also, since 2026-09-04, `execution_reported` rows: fills IBKR reported on the execution WebSocket and the session showed (any origin) — a broker event, in no replay allowlist. Full type list: `ConversationStore.add_decision`'s docstring. |
| `doc_versions` | Versioned snapshots of `context.md` + `principles.md` — full text, hash, date |

(A `relationships` table and a decisions FTS index were removed 2026-07-03 — never wired to
any caller; symbol-level knowledge belongs to the planned knowledge layer. Existing DBs are
migrated safely: derived index dropped, `relationships` dropped only if empty.)

**Search:** ClaudIA uses SQLite FTS5 to search conversation history. Ask: *"What did we
discuss about NVDA last month?"* The `search_past_conversations` tool searches `messages_fts`
— it does **not** join to `sessions`/`doc_version`, so results do not include which document
version was active at the time. (`get_decisions_for_symbol` does join to `doc_version`, but
that method isn't exposed as an LLM tool today.)

**Two exclusions, both deliberate** (this said "all messages across all sessions" until
2026-09-23, when both were added):

- **The live session** (gap #30). A recall probe once returned the user's own question from
  seconds earlier. Harmless for a user message; the concern is an *assistant* message from the
  same session coming back as independent corroborating history, which is the self-confirmation
  the anti-fabrication layers exist to prevent — a claim needs evidence from **outside** the
  turn that made it. Excluded rather than labelled: labelling would ask the model to police
  itself on the one path that exists because it cannot.
- **`tool` rows** (gap #21). A tool row is plumbing, not conversation. Model-initiated rows
  write `content=""` and index nothing, but an **out-of-loop** row carries an origin stamp
  there — `ui_button` for a click, `startup` for a background task. `startup` is an ordinary
  English word, so without the filter a recall search would hand the model its own plumbing
  as "past conversation". Search hits reach the model as a `tool_result`, which the safety
  block names a guaranteed source, so the bar is higher than mere noise.

Both are applied in `WHERE`, before `LIMIT`, so filtering never silently shortens the result
set. The rows themselves are untouched and stay available to an audit reading the database.

**Version snapshots** are also written to `docs/versions/{label}/` as human-readable files for
reference.

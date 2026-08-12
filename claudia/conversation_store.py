"""Persistent conversation store for ClaudIA.

Tables:
  sessions     — one row per chat session
  messages     — full conversation history (user / assistant / tool)
  decisions    — extracted trade decisions and key moments
  doc_versions — versioned snapshots of context.md + principles.md

An FTS5 virtual table on messages.content enables "what did we discuss about
NVDA last month?" without a vector DB. (A relationships table and a decisions
FTS index existed until 2026-07-03 but never had a caller — removed per the
info-architecture review, finding M2; symbol-level knowledge belongs to the
planned knowledge layer.)
"""

import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


RENDERED_PROPOSAL_TYPES: tuple[str, ...] = (
    "trade_proposed",
    "trade_cancel_proposed",
    "trade_modify_proposed",
)
"""The decision types that mean **a staging button reached the user**.

Deliberately an allowlist rather than "everything except `proposal_render_failed`". A
denylist admits every future decision type by default, and the one thing
`get_rendered_proposals` must never do is report a proposal as emitted when no button
exists — that is the false claim the whole guardrail was built to remove. A new type has
to be added here consciously.

`claudia/agent.py` maps these onto the `propose_*` tool names for the replayed record; the
two are pinned to each other by
tests/test_agent.py::test_emission_record_tools_cover_exactly_the_store_allowlist.
"""


COMPLETED_ORDER_ACTION_TYPES: tuple[str, ...] = (
    "trade_staged",
    "trade_cancelled",
    "trade_modified",
)
"""The decision types that mean **a write really reached IBKR**.

Written only by `claudia/order_flow.py`, after a physical button click has passed Gate 1
(Touch ID) and Gate 2 (the AppKit dialog) and the dispatch call has returned. They are a
strictly stronger fact than `RENDERED_PROPOSAL_TYPES` — which say only that a button was
drawn — and the two allowlists are deliberately disjoint (pinned by
tests/test_conversation_store.py::test_completed_and_rendered_allowlists_are_disjoint).

Why they have to be replayed to the model: a click produces no tool call and no assistant
message, so a completed staging leaves *no* trace in the transcript `_history_to_messages`
rebuilds. On 2026-07-27 that blindness let ClaudIA tell the user "there is nothing to
cancel — the order was only ever a staged button" minutes after an ES order had been
staged, confirmed `Submitted`, and recorded here.
"""


def _utcnow() -> str:
    """Current UTC time as an ISO-8601 string — the storage format for every timestamp."""
    return datetime.now(UTC).isoformat()


def _fts_query(text: str) -> str:
    """Turn arbitrary user text into a valid FTS5 query. "" when there is nothing to search.

    **FTS5 `MATCH` takes a query expression, not a search string.** Passing raw text through
    is a syntax error for most of what a person actually types — measured against the live
    store on 2026-08-05, 7 of 8 realistic queries raised `sqlite3.OperationalError`::

        "AAPL (long)"        fts5: syntax error near "AAPL"
        "note: something"    no such column: note
        "-AAPL"              no such column: AAPL
        "C++ risk"           fts5: syntax error near "+"
        "what's the P&L?"    fts5: syntax error near "'"

    Every token is extracted and quoted, which makes punctuation inert: a double-quoted
    FTS5 string is a literal, so `(`, `:`, `-` and `+` can no longer be read as operators.

    The quoting cannot be broken out of, and that is structural rather than careful: `\\w+`
    matches no `"`, so no token can contain the one character that would close the literal
    early. There is no escaping step here to get wrong — the only way in would be to widen
    that pattern.

    ## Why OR, not AND — measured, not preferred

    FTS5's implicit connective for adjacent terms is AND, so preserving the old semantics
    was the smaller change. It is also the wrong one. Over the live store (646 messages),
    AND returned **zero rows** for `NVDA position` and for `did we discuss the dashboard?`
    — ordinary questions about subjects the store demonstrably contains. Zero rows makes
    the tool answer "No past conversations found", which is a false negative reported as a
    fact, and this codebase treats a confident wrong answer as worse than a vague one.

    OR always returns candidates and lets bm25 (`ORDER BY rank`) do the discriminating,
    which is what the caller's `LIMIT` consumes — the row *count* is irrelevant, only the
    top few matter. Measured share of the top 5 that were genuinely relevant: `what's the
    P&L?` 5/5, `order staging` 5/5, `flex sync` 5/5, `C++ risk` 5/5, `NVDA position` 2/5.

    Dropping short tokens was tried and rejected on the same evidence: it made `what's the
    P&L?` *worse* (5/5 to 1/5), because `p` and `l` are the signal in that query. There is
    no stopword list here for the same reason — it is a list to maintain that the
    measurement did not justify.

    Residual, accepted and not papered over: a question dominated by common words can still
    rank badly (`did we discuss the dashboard?` scored 0/5). That is a relevance limit, not
    a crash, and it reports honestly as "no relevant matches" rather than as an error.

    Args:
        text: Whatever the caller typed or the model passed through.

    Returns:
        A quoted OR-expression, or "" when the text holds no searchable token at all
        (`"?!"`) — the caller must treat that as "nothing to search", not as a query.
    """
    return " OR ".join(f'"{token}"' for token in re.findall(r"\w+", text))


class ConversationStore:
    """SQLite-backed conversation memory: sessions, messages, decisions, doc versions.

    Connection-per-operation (see `_conn`) rather than one long-lived handle, so the store
    can be shared process-wide across every Panel session without handle contention. WAL
    mode makes that safe alongside `GDriveSync.upload_db()`, which reads the same file from
    a separate thread at session stop while the main loop may still be reading history.

    Full-text search over messages is provided by an FTS5 virtual table kept in sync by
    triggers; see `_init_schema`.
    """

    def __init__(self, db_path: str | Path = "data/claudia.db"):
        """Open (or create) the SQLite DB and apply schema migrations.

        WAL mode and foreign-key enforcement are set per connection in _conn().
        The doc_version column migration runs at init time with suppress so it is
        a no-op on DBs that already have the column.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured connection, committing on success and rolling back on error.

        Yields:
            A connection with `row_factory=sqlite3.Row`, WAL journaling, and foreign keys
            enabled. Closed on exit either way; exceptions roll back and re-raise rather
            than being swallowed, so a partial write never looks like a success.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # WAL mode allows concurrent readers during a write — required because
        # GDriveSync.upload_db() opens the DB in a separate thread (at session stop)
        # while the main loop may still be reading history for a pending response.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        """Create the schema if absent. Idempotent — safe on every startup.

        Tables: `sessions` (one row per browser session, carrying the context hash and doc
        version), `messages` (user/assistant/tool turns), `decisions` (order proposals and
        their outcomes), and `doc_versions` (hash-keyed context/principles history).
        Plus a `messages_fts` FTS5 virtual table and indexes on the session foreign keys.

        **The FTS index has insert and delete triggers only — there is deliberately no
        update trigger**, and this docstring claimed one until 2026-08-05. Nothing in the
        package issues `UPDATE messages` (a message row is written once and never edited),
        so an `messages_au` trigger would be dead code on an external-content FTS table.
        The consequence of that being wrong is silent — the index would simply drift from
        the table with no error — so if an update path is ever added, the trigger has to be
        added with it. `tests/test_conversation_store.py` pins both facts.

        Everything uses `CREATE ... IF NOT EXISTS`; the one additive column migration is
        wrapped in `suppress(sqlite3.OperationalError)` so re-running against an
        already-migrated DB is a no-op rather than an error.
        """
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id           TEXT PRIMARY KEY,
                    started_at   TEXT NOT NULL,
                    ended_at     TEXT,
                    context_hash TEXT,
                    metadata     TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id       TEXT NOT NULL REFERENCES sessions(id),
                    role             TEXT NOT NULL CHECK(role IN ('user','assistant','tool')),
                    content          TEXT,
                    tool_name        TEXT,
                    tool_input_json  TEXT,
                    tool_result_json TEXT,
                    created_at       TEXT NOT NULL,
                    tokens_used      INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS decisions (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id    TEXT NOT NULL REFERENCES sessions(id),
                    message_id    INTEGER REFERENCES messages(id),
                    decision_type TEXT NOT NULL,
                    symbol        TEXT,
                    summary_text  TEXT NOT NULL,
                    metadata_json TEXT DEFAULT '{}',
                    created_at    TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_messages_session
                    ON messages(session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_decisions_symbol
                    ON decisions(symbol, created_at);

                CREATE TABLE IF NOT EXISTS doc_versions (
                    version         TEXT PRIMARY KEY,
                    context_hash    TEXT UNIQUE NOT NULL,
                    context_text    TEXT NOT NULL,
                    principles_text TEXT NOT NULL,
                    created_at      TEXT NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                    USING fts5(content, content=messages, content_rowid=id);

                -- Keep FTS index in sync
                CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content)
                        VALUES ('delete', old.id, old.content);
                END;
            """)
        # Migration for existing DBs that predate the doc_version column
        with self._conn() as conn, suppress(sqlite3.OperationalError):
            conn.execute("ALTER TABLE sessions ADD COLUMN doc_version TEXT")
        # Migration: drop dead schema (2026-07-03 review finding M2 — no caller
        # ever existed for relationships or decisions FTS search).
        # Triggers first, or decisions writes would reference a dropped table.
        # decisions_fts is a derived index (content=decisions) — rebuildable, safe.
        # relationships is dropped only if provably empty; data is never destroyed.
        with self._conn() as conn:
            conn.executescript("""
                DROP TRIGGER IF EXISTS decisions_ai;
                DROP TRIGGER IF EXISTS decisions_ad;
                DROP TABLE IF EXISTS decisions_fts;
            """)
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='relationships'"
            ).fetchone()
            if exists:
                count = conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
                if count == 0:
                    conn.executescript(
                        "DROP INDEX IF EXISTS idx_relationships_symbol;"
                        "DROP TABLE relationships;"
                    )
                else:
                    log.warning(
                        "relationships table contains %d rows — kept (schema is "
                        "otherwise retired; expected empty since no writer ever existed)",
                        count,
                    )

    # ── Sessions ──────────────────────────────────────────────────────────────

    def create_session(
        self, session_id: str, context_hash: str = "", doc_version: str | None = None
    ) -> None:
        """Insert a new session row. INSERT OR IGNORE is a no-op if called twice for the same id."""
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sessions(id, started_at, context_hash, doc_version) "
                "VALUES (?,?,?,?)",
                (session_id, _utcnow(), context_hash, doc_version),
            )

    def close_session(self, session_id: str, metadata: dict | None = None) -> None:
        """Stamp ended_at and write session metadata (tool counts, connectivity) to the row."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at=?, metadata=? WHERE id=?",
                (_utcnow(), json.dumps(metadata or {}), session_id),
            )

    def get_last_context_hash(self) -> str | None:
        """Return context_hash from the most recently started session, or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT context_hash FROM sessions "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return row["context_hash"] if row else None

    # ── Doc versions ──────────────────────────────────────────────────────────

    def register_doc_version_if_new(
        self, context_hash: str, context_text: str, principles_text: str
    ) -> str:
        """Register a new version if hash is unknown. Returns version label (e.g. 'v1')."""
        with self._conn() as conn:
            if row := conn.execute(
                "SELECT version FROM doc_versions WHERE context_hash = ?", (context_hash,)
            ).fetchone():
                return str(row["version"])  # sqlite3.Row.__getitem__ is typed Any; column is TEXT
            count = conn.execute("SELECT COUNT(*) FROM doc_versions").fetchone()[0]
            version = f"v{count + 1}"
            conn.execute(
                "INSERT INTO doc_versions "
                "(version, context_hash, context_text, principles_text, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (version, context_hash, context_text, principles_text, _utcnow()),
            )
            return version

    def get_version_label(self, context_hash: str) -> str | None:
        """Return version label for a given hash, or None if unregistered."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT version FROM doc_versions WHERE context_hash = ?", (context_hash,)
            ).fetchone()
            return row["version"] if row else None

    def get_doc_version(self, version: str) -> dict | None:
        """Return full snapshot for a version label, or None if not found."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT version, context_text, principles_text, created_at "
                "FROM doc_versions WHERE version = ?",
                (version,),
            ).fetchone()
            return dict(row) if row else None

    def list_doc_versions(self) -> list[dict]:
        """Return all registered versions ordered oldest first."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT version, context_hash, created_at FROM doc_versions "
                "ORDER BY created_at ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_session(self, session_id: str) -> dict | None:
        """Return a single session row as a dict, or None if the id is unknown."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_sessions(self, limit: int = 20) -> list[dict]:
        """Return the most recent sessions, newest first."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Messages ──────────────────────────────────────────────────────────────

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str = "",
        tool_name: str | None = None,
        tool_input: dict | None = None,
        tool_result: Any = None,
        tokens_used: int = 0,
    ) -> int:
        """Insert a message row and return its primary key.

        The returned id is used as the message_id foreign key in decisions —
        callers that surface a trade proposal must pass it to add_decision().
        """
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO messages
                   (session_id, role, content, tool_name,
                    tool_input_json, tool_result_json, created_at, tokens_used)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    session_id,
                    role,
                    content,
                    tool_name,
                    json.dumps(tool_input, default=str) if tool_input is not None else None,
                    json.dumps(tool_result, default=str) if tool_result is not None else None,
                    _utcnow(),
                    tokens_used,
                ),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def get_history(self, session_id: str, limit: int = 50) -> list[dict]:
        """Return recent conversation messages for context injection."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM (
                       SELECT * FROM messages
                       WHERE session_id=?
                       ORDER BY created_at DESC LIMIT ?
                   ) ORDER BY created_at ASC""",
                (session_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def count_messages(self, session_id: str) -> int:
        """Return the total number of messages stored for a session."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id=?", (session_id,)
            ).fetchone()
            return row[0] if row else 0

    def search_messages(self, query: str, max_results: int = 10, max_tokens: int = 2000) -> list[dict]:
        """FTS5 full-text search across all conversation history.

        `query` is arbitrary user/model text and is converted by `_fts_query` before it
        reaches `MATCH`, which takes a query *expression* — raw text is a syntax error for
        most of what anyone actually types, and that error used to escape this method and
        kill the whole turn. See `_fts_query` for the measurement and for why the tokens
        are OR-ed rather than AND-ed.

        Returns [] when the text holds no searchable token, rather than running a query
        that cannot match anything.

        max_tokens is a rough budget: results are trimmed when the cumulative
        character count exceeds max_tokens * 4 (i.e. ~4 chars per token, not
        exact token counting).
        """
        expression = _fts_query(query)
        if not expression:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT m.*, highlight(messages_fts, 0, '[', ']') AS snippet
                   FROM messages_fts
                   JOIN messages m ON m.id = messages_fts.rowid
                   WHERE messages_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (expression, max_results),
            ).fetchall()
            results = [dict(r) for r in rows]
        # Rough token budget guard
        total = 0
        trimmed = []
        for r in results:
            text = r.get("content") or ""
            total += len(text) // 4
            if total > max_tokens:
                break
            trimmed.append(r)
        return trimmed

    # ── Decisions ─────────────────────────────────────────────────────────────

    def add_decision(
        self,
        session_id: str,
        decision_type: str,
        summary_text: str,
        symbol: str | None = None,
        message_id: int | None = None,
        metadata: dict | None = None,
    ) -> int:
        """Record a trade proposal and return its primary key.

        decision_type is a free-form label; it is not validated by a CHECK constraint, so
        callers must use consistent values. Every value claudia writes today:

        From `claudia/agent.py` — a proposal was *surfaced* (the user has not decided yet):
          - ``trade_proposed`` — a new-order staging button was rendered for the user
          - ``trade_cancel_proposed`` — a cancel staging button was rendered
          - ``trade_modify_proposed`` — a modify staging button was rendered
          - ``proposal_render_failed`` — a proposal was accepted but **no button reached
            the user**; nothing was staged and no order exists
          - ``proposal_claim_unbacked`` — the assistant text claimed a completed order
            action while **no proposal tool was called at all**; nothing was staged and no
            order exists
          - ``book_claim_unverified`` — the assistant text claimed a check of the live
            order book while **no order-book tool was called**; whatever order state that
            message stated is unverified

        From `claudia/order_flow.py` — the user clicked and both gates passed:
          - ``trade_staged`` / ``trade_cancelled`` / ``trade_modified`` — the write reached
            IBKR. These are `COMPLETED_ORDER_ACTION_TYPES`, replayed to the model by
            `get_completed_order_actions`; their `metadata` carries `ibkr_order_id`,
            `readback_confirmed` and `readback_order_status`, and the record is only as
            honest as those three fields.

        The three failure types are deliberately their own types rather than flags on the
        three proposal types: those must keep meaning "a button was shown", or every
        historical row and every regression baseline silently changes meaning. They are
        also distinct from each other, because they describe different states of the tool
        loop and no query or report may conflate them:
        `ClaudIAAgent._emit_guardrail_notice` writes `proposal_render_failed` (accepted,
        never drawn — the 2026-07-17 / 2026-07-24 failures),
        `ClaudIAAgent._emit_unbacked_claim_notice` writes `proposal_claim_unbacked` (never
        proposed, only narrated), and `ClaudIAAgent._emit_stale_book_claim_notice` writes
        `book_claim_unverified` (never looked, only narrated) — the last two are the two
        halves of the single 2026-07-28 failure message, and one turn can earn both. All
        three are excluded from every allowlist here: replaying any of them as evidence
        would assert, on the channel the model cannot forge, the exact falsehood the notice
        removes.

        message_id should be the id returned by add_message() for the assistant turn that
        surfaced the proposal — for both failure types, the turn carrying the claim the
        notice contradicts.
        """
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO decisions
                   (session_id, message_id, decision_type, symbol,
                    summary_text, metadata_json, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    session_id,
                    message_id,
                    decision_type,
                    symbol,
                    summary_text,
                    json.dumps(metadata or {}, default=str),
                    _utcnow(),
                ),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def get_decisions(self, session_id: str) -> list[dict]:
        """Return all decisions recorded for a session, oldest first."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE session_id=? ORDER BY created_at",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_rendered_proposals(self, session_id: str) -> list[dict]:
        """Return the session's proposals that really rendered a staging button, oldest first.

        The source for the proposal-emission records `claudia/agent.py` replays on the
        operator channel. `_history_to_messages` drops tool rows, so from turn N+1 the model
        has no evidence it ever called `propose_order` on turn N — the blindness behind
        ClaudIA telling a user a modify proposal was "sitting in front of you" when no
        button had ever been rendered.

        Two filters, both load-bearing:

        - `decision_type IN RENDERED_PROPOSAL_TYPES` — an allowlist. `proposal_render_failed`
          means "accepted but **no** button reached the user", so replaying one as an
          emission record would assert the exact falsehood the guardrail removes. Pinned by
          test_get_rendered_proposals_never_returns_a_render_failure.
        - `message_id IS NOT NULL` — the emission is only meaningful as something a
          specific assistant turn produced.

        Returns:
            Row dicts with `metadata_json` **decoded** into a `metadata` dict — unlike
            `get_decisions`, which returns the raw column. Callers here read
            `metadata["order"]["order_id"]`, and every one of them would otherwise repeat
            the same parse. Ordering and malformed-metadata handling are
            `_decisions_of_types`'.
        """
        return self._decisions_of_types(
            session_id, RENDERED_PROPOSAL_TYPES, require_message_id=True,
        )

    def get_completed_order_actions(self, session_id: str) -> list[dict]:
        """Return the session's order writes that really reached IBKR, oldest first.

        The source for the completed-action records `claudia/agent.py` replays on the
        operator channel, alongside the proposal records. Staging happens by *button
        click* — no tool call, no assistant message — so a completed order is invisible in
        the replayed transcript. That is what let ClaudIA state, minutes after an ES order
        was staged and confirmed `Submitted`, that "there is nothing to cancel"
        (2026-07-27, live).

        Two deliberate differences from `get_rendered_proposals`:

        - The allowlist is `COMPLETED_ORDER_ACTION_TYPES` — the post-click types only.
        - **No `message_id` filter.** `order_flow` writes these rows without one, because a
          click belongs to no assistant turn. Requiring one here would return nothing and
          leave the model exactly as blind as it was during the incident. Pinned by
          test_get_completed_order_actions_does_not_require_a_message_id.

        Returns:
            Row dicts with `metadata_json` decoded into `metadata` (see
            `get_rendered_proposals` for the same contract). Callers read
            `metadata["ibkr_order_id"]`, `["readback_confirmed"]` and
            `["readback_order_status"]`.
        """
        return self._decisions_of_types(
            session_id, COMPLETED_ORDER_ACTION_TYPES, require_message_id=False,
        )

    def get_called_tool_names(self, session_id: str) -> list[str]:
        """Return the distinct names of every tool called in a session, sorted alphabetically.

        The source for the called-tool ledger `claudia/agent.py` replays on the operator
        channel. `_history_to_messages` drops tool rows — it has to, since the DB stores no
        Anthropic `tool_use_id`s and orphaned `tool_result` blocks are a 400 — so from turn
        N+1 the model holds no tool payload at all, only its own earlier prose about them.
        Its docstring assumed that prose was enough. Measured 2026-08-11: asked for chart
        settings its own message had recorded only by study *name*, it invented colours,
        line widths and precision — terms with zero occurrences anywhere in this database.
        The ledger does not restore the payloads; it tells the model they are gone.

        **Names only, and that is a safety boundary, not a simplification.** This method
        must never grow to return `tool_input_json` or `tool_result_json`: a tool input can
        carry an account number, an order id or a position, and everything returned here
        goes into the model's context and the outgoing request body. A tool name is safe;
        anything beside it is a new exposure.

        Sorted and deduped for **byte-stability**, the same argument as
        `get_rendered_proposals`': the ledger is rebuilt every turn and lands after the
        cached prefix, so calling a tool a second time must not change a byte of it.
        Chronological order or a call count would move the block for no new fact.

        NULL and blank names are filtered defensively (`tool_name` is nullable): a blank
        line would name no tool while still asserting that one was called.

        Whole-session, deliberately unlike `get_history`'s `limit`: the claim being made is
        "you called this earlier in this session", and a tool whose surrounding turns have
        scrolled out of the replayed history is precisely the one the model has least
        evidence about. Capping this to the history window would drop it first.

        Returns:
            Distinct non-blank `tool_name`s of this session's `role='tool'` rows, ascending.
            Empty when the session has called none.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT DISTINCT tool_name FROM messages
                   WHERE session_id=? AND role='tool'
                     AND tool_name IS NOT NULL AND TRIM(tool_name) <> ''
                   ORDER BY tool_name""",
                (session_id,),
            ).fetchall()
        return [row["tool_name"] for row in rows]

    def _decisions_of_types(
        self, session_id: str, types: tuple[str, ...], *, require_message_id: bool,
    ) -> list[dict]:
        """Decision rows of the given types for one session, oldest first, metadata decoded.

        Shared by the two operator-channel queries above, which differ only in their
        allowlist and in whether a `message_id` is required — see each for why.

        Ordered by `id`, not `created_at`: two rows written in the same turn can share a
        timestamp, and the replayed record has to be byte-identical across calls or it
        would thrash the request body turn after turn.

        A row whose `metadata_json` does not parse yields `{}` rather than raising: one
        malformed row must not take down a turn, and the record simply loses its order id
        (it never gains a wrong one).
        """
        placeholders = ",".join("?" * len(types))
        message_id_clause = "AND message_id IS NOT NULL" if require_message_id else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT * FROM decisions
                    WHERE session_id=? {message_id_clause}
                      AND decision_type IN ({placeholders})
                    ORDER BY id""",
                (session_id, *types),
            ).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            raw = record.pop("metadata_json", None)
            try:
                parsed = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                log.warning(
                    "decision %s has unparseable metadata_json; operator-channel record "
                    "will carry no order id", record.get("id"),
                )
                parsed = {}
            record["metadata"] = parsed if isinstance(parsed, dict) else {}
            out.append(record)
        return out

    def get_decisions_for_symbol(self, symbol: str, limit: int = 10) -> list[dict]:
        """Return decisions for a symbol ordered newest first, joined with the doc_version active at the time."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT d.*, s.doc_version FROM decisions d
                   JOIN sessions s ON s.id = d.session_id
                   WHERE d.symbol=?
                   ORDER BY d.created_at DESC LIMIT ?""",
                (symbol, limit),
            ).fetchall()
            return [dict(r) for r in rows]


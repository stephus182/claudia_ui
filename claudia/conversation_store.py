"""
Persistent conversation store for ClaudIA.

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
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


log = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConversationStore:
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
        with self._conn() as conn:
            with suppress(sqlite3.OperationalError):
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

        max_tokens is a rough budget: results are trimmed when the cumulative
        character count exceeds max_tokens * 4 (i.e. ~4 chars per token, not
        exact token counting).
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT m.*, highlight(messages_fts, 0, '[', ']') AS snippet
                   FROM messages_fts
                   JOIN messages m ON m.id = messages_fts.rowid
                   WHERE messages_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (query, max_results),
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

        decision_type is a free-form label (e.g. "order_proposal", "alert"); it
        is not validated by a CHECK constraint so callers must use consistent
        values. message_id should be the id returned by add_message() for the
        assistant turn that surfaced the proposal.
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


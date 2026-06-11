"""
Persistent conversation store for ClaudIA.

Tables:
  sessions     — one row per chat session
  messages     — full conversation history (user / assistant / tool)
  decisions    — extracted trade decisions and key moments
  relationships — accumulated symbol-level insights over time
  doc_versions  — versioned snapshots of context.md + principles.md

FTS5 virtual tables on messages.content and decisions.summary_text
enable "what did I decide about NVDA last month?" without a vector DB.
"""

import json
import sqlite3
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConversationStore:
    def __init__(self, db_path: str | Path = "data/claudia.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
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

                CREATE TABLE IF NOT EXISTS relationships (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol           TEXT NOT NULL,
                    observation_type TEXT NOT NULL,
                    content          TEXT NOT NULL,
                    session_id       TEXT REFERENCES sessions(id),
                    created_at       TEXT NOT NULL,
                    relevance_score  REAL DEFAULT 1.0
                );

                CREATE INDEX IF NOT EXISTS idx_messages_session
                    ON messages(session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_decisions_symbol
                    ON decisions(symbol, created_at);
                CREATE INDEX IF NOT EXISTS idx_relationships_symbol
                    ON relationships(symbol, created_at);

                CREATE TABLE IF NOT EXISTS doc_versions (
                    version         TEXT PRIMARY KEY,
                    context_hash    TEXT UNIQUE NOT NULL,
                    context_text    TEXT NOT NULL,
                    principles_text TEXT NOT NULL,
                    created_at      TEXT NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                    USING fts5(content, content=messages, content_rowid=id);
                CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts
                    USING fts5(summary_text, content=decisions, content_rowid=id);

                -- Keep FTS indexes in sync
                CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content)
                        VALUES ('delete', old.id, old.content);
                END;
                CREATE TRIGGER IF NOT EXISTS decisions_ai AFTER INSERT ON decisions BEGIN
                    INSERT INTO decisions_fts(rowid, summary_text)
                        VALUES (new.id, new.summary_text);
                END;
                CREATE TRIGGER IF NOT EXISTS decisions_ad AFTER DELETE ON decisions BEGIN
                    INSERT INTO decisions_fts(decisions_fts, rowid, summary_text)
                        VALUES ('delete', old.id, old.summary_text);
                END;
            """)
        # Migration for existing DBs that predate the doc_version column
        with self._conn() as conn:
            with suppress(sqlite3.OperationalError):
                conn.execute("ALTER TABLE sessions ADD COLUMN doc_version TEXT")

    # ── Sessions ──────────────────────────────────────────────────────────────

    def create_session(
        self, session_id: str, context_hash: str = "", doc_version: str | None = None
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sessions(id, started_at, context_hash, doc_version) "
                "VALUES (?,?,?,?)",
                (session_id, _utcnow(), context_hash, doc_version),
            )

    def close_session(self, session_id: str, metadata: dict | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at=?, metadata=? WHERE id=?",
                (_utcnow(), json.dumps(metadata or {}), session_id),
            )

    def get_last_context_hash(self) -> str | None:
        """Return context_hash from the most recently completed session, or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT context_hash FROM sessions "
                "WHERE ended_at IS NOT NULL "
                "ORDER BY ended_at DESC LIMIT 1"
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
                return row["version"]
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
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_sessions(self, limit: int = 20) -> list[dict]:
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
                    json.dumps(tool_input) if tool_input is not None else None,
                    json.dumps(tool_result) if tool_result is not None else None,
                    _utcnow(),
                    tokens_used,
                ),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def get_history(self, session_id: str, limit: int = 50) -> list[dict]:
        """Return recent conversation messages for context injection."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM messages
                   WHERE session_id=?
                   ORDER BY created_at DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def search_messages(self, query: str, max_results: int = 10, max_tokens: int = 2000) -> list[dict]:
        """FTS5 full-text search across all conversation history."""
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
                    json.dumps(metadata or {}),
                    _utcnow(),
                ),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def search_decisions(self, query: str, max_results: int = 5) -> list[dict]:
        """FTS5 search across trade decisions. Results include doc_version from the session."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT d.*, s.doc_version, highlight(decisions_fts, 0, '[', ']') AS snippet
                   FROM decisions_fts
                   JOIN decisions d ON d.id = decisions_fts.rowid
                   JOIN sessions s ON s.id = d.session_id
                   WHERE decisions_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (query, max_results),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_decisions_for_symbol(self, symbol: str, limit: int = 10) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT d.*, s.doc_version FROM decisions d
                   JOIN sessions s ON s.id = d.session_id
                   WHERE d.symbol=?
                   ORDER BY d.created_at DESC LIMIT ?""",
                (symbol, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Relationships ─────────────────────────────────────────────────────────

    def add_relationship(
        self,
        symbol: str,
        observation_type: str,
        content: str,
        session_id: str | None = None,
        relevance_score: float = 1.0,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO relationships
                   (symbol, observation_type, content, session_id, created_at, relevance_score)
                   VALUES (?,?,?,?,?,?)""",
                (symbol, observation_type, content, session_id, _utcnow(), relevance_score),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def get_relationships(self, symbol: str, limit: int = 10) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM relationships WHERE symbol=?
                   ORDER BY relevance_score DESC, created_at DESC LIMIT ?""",
                (symbol, limit),
            ).fetchall()
            return [dict(r) for r in rows]

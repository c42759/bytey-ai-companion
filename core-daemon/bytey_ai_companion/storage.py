"""SQLite persistence layer for sessions, messages, and MCP tool calls."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB_FILENAME = "bytey.db"
LEGACY_DB_FILENAME = "pet.db"


def resolve_data_dir() -> Path:
    """Resolve the persistent data directory, honoring BYTEY_DATA_DIR / AI_PET_DATA_DIR and legacy fallback."""
    env_dir = os.environ.get("BYTEY_DATA_DIR") or os.environ.get("AI_PET_DATA_DIR")
    if env_dir:
        return Path(env_dir)
    primary = Path.home() / ".local" / "share" / "bytey-ai-companion"
    legacy = Path.home() / ".local" / "share" / "ai-pet"
    if (primary / DEFAULT_DB_FILENAME).exists() or (primary / LEGACY_DB_FILENAME).exists():
        return primary
    if (legacy / DEFAULT_DB_FILENAME).exists() or (legacy / LEGACY_DB_FILENAME).exists():
        return legacy
    return primary


def resolve_db_path(data_dir: Path | None = None) -> Path:
    """Resolve SQLite database path, falling back to legacy pet.db when present."""
    base = Path(data_dir) if data_dir is not None else resolve_data_dir()
    primary_db = base / DEFAULT_DB_FILENAME
    legacy_db = base / LEGACY_DB_FILENAME
    if primary_db.exists():
        return primary_db
    if legacy_db.exists():
        return legacy_db
    return primary_db


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StorageManager:
    """Async SQLite manager backed by sqlite3 and asyncio.to_thread."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = resolve_db_path() if db_path is None else Path(db_path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=30.0,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA journal_mode = WAL;")
            self._conn = conn
        return self._conn

    def _close_sync(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    async def close(self) -> None:
        """Close the underlying SQLite connection."""
        await asyncio.to_thread(self._close_sync)

    def _init_db_sync(self) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    input_mode TEXT NOT NULL DEFAULT 'text',
                    content TEXT NOT NULL,
                    emotion TEXT NOT NULL DEFAULT 'neutral',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS tool_calls (
                    id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    server_name TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL DEFAULT '{}',
                    result_summary TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'running',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_messages_session_created
                    ON messages(session_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_tool_calls_message_created
                    ON tool_calls(message_id, created_at);
                """
            )
            conn.commit()

    async def init_db(self) -> None:
        """Create database directories, tables, and indexes if missing."""
        await asyncio.to_thread(self._init_db_sync)

    def _create_session_locked(
        self, conn: sqlite3.Connection, title: str
    ) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        now = _utc_now_iso()
        conn.execute(
            """
            INSERT INTO sessions (id, title, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, title, now, now),
        )
        conn.commit()
        return {
            "id": session_id,
            "title": title,
            "created_at": now,
            "updated_at": now,
        }

    def _get_or_create_active_session_sync(self) -> dict[str, Any]:
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                """
                SELECT id, title, created_at, updated_at
                FROM sessions
                ORDER BY updated_at DESC, rowid DESC
                LIMIT 1
                """
            ).fetchone()
            if row is not None:
                return dict(row)
            return self._create_session_locked(conn, "New Chat")

    async def get_or_create_active_session(self) -> dict[str, Any]:
        """Return the most recently active session, creating one if none exist."""
        return await asyncio.to_thread(self._get_or_create_active_session_sync)

    def _create_session_sync(self, title: str) -> dict[str, Any]:
        with self._lock:
            conn = self._get_conn()
            return self._create_session_locked(conn, title)

    async def create_session(self, title: str = "New Chat") -> dict[str, Any]:
        """Create and persist a new chat session."""
        return await asyncio.to_thread(self._create_session_sync, title)

    def _list_sessions_sync(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                """
                SELECT id, title, created_at, updated_at
                FROM sessions
                ORDER BY updated_at DESC, rowid DESC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    async def list_sessions(self) -> list[dict[str, Any]]:
        """List all sessions ordered by most recently updated first."""
        return await asyncio.to_thread(self._list_sessions_sync)

    def _delete_session_sync(self, session_id: str) -> bool:
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
            return cur.rowcount > 0

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session and cascade-delete its messages and tool calls."""
        return await asyncio.to_thread(self._delete_session_sync, session_id)

    def _clear_session_messages_sync(self, session_id: str) -> int:
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (_utc_now_iso(), session_id),
            )
            conn.commit()
            return cur.rowcount

    async def clear_session_messages(self, session_id: str) -> int:
        """Delete all messages (and cascaded tool calls) in a session."""
        return await asyncio.to_thread(self._clear_session_messages_sync, session_id)

    def _add_message_sync(
        self,
        session_id: str,
        role: str,
        content: str,
        input_mode: str,
        emotion: str,
        message_id: str | None,
    ) -> dict[str, Any]:
        mid = message_id or str(uuid.uuid4())
        now = _utc_now_iso()
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT INTO messages (id, session_id, role, input_mode, content, emotion, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (mid, session_id, role, input_mode, content, emotion, now),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            conn.commit()
        return {
            "id": mid,
            "session_id": session_id,
            "role": role,
            "input_mode": input_mode,
            "content": content,
            "emotion": emotion,
            "created_at": now,
            "tool_calls": [],
        }

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        input_mode: str = "text",
        emotion: str = "neutral",
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """Insert a message into a session and refresh the session timestamp."""
        return await asyncio.to_thread(
            self._add_message_sync,
            session_id,
            role,
            content,
            input_mode,
            emotion,
            message_id,
        )

    def _update_message_sync(
        self, message_id: str, content: str, emotion: str | None
    ) -> dict[str, Any] | None:
        now = _utc_now_iso()
        with self._lock:
            conn = self._get_conn()
            if emotion is not None:
                cur = conn.execute(
                    "UPDATE messages SET content = ?, emotion = ? WHERE id = ?",
                    (content, emotion, message_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE messages SET content = ? WHERE id = ?",
                    (content, message_id),
                )
            if cur.rowcount == 0:
                conn.commit()
                return None

            row = conn.execute(
                """
                SELECT id, session_id, role, input_mode, content, emotion, created_at
                FROM messages
                WHERE id = ?
                """,
                (message_id,),
            ).fetchone()
            if row is None:
                conn.commit()
                return None

            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, row["session_id"]),
            )
            calls = conn.execute(
                """
                SELECT id, message_id, server_name, tool_name, arguments_json,
                       result_summary, status, created_at
                FROM tool_calls
                WHERE message_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (message_id,),
            ).fetchall()
            conn.commit()

            msg = dict(row)
            msg["tool_calls"] = [dict(c) for c in calls]
            return msg

    async def update_message(
        self, message_id: str, content: str, emotion: str | None = None
    ) -> dict[str, Any] | None:
        """Update message content and optionally emotion."""
        return await asyncio.to_thread(
            self._update_message_sync, message_id, content, emotion
        )

    def _list_messages_sync(self, session_id: str, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                """
                SELECT id, session_id, role, input_mode, content, emotion, created_at
                FROM (
                    SELECT id, session_id, role, input_mode, content, emotion,
                           created_at, rowid AS _rid
                    FROM messages
                    WHERE session_id = ?
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT ?
                )
                ORDER BY created_at ASC, _rid ASC
                """,
                (session_id, limit),
            ).fetchall()
            messages = [dict(row) for row in rows]
            if not messages:
                return []

            msg_ids = [m["id"] for m in messages]
            placeholders = ",".join("?" for _ in msg_ids)
            call_rows = conn.execute(
                f"""
                SELECT id, message_id, server_name, tool_name, arguments_json,
                       result_summary, status, created_at
                FROM tool_calls
                WHERE message_id IN ({placeholders})
                ORDER BY created_at ASC, rowid ASC
                """,
                msg_ids,
            ).fetchall()

            calls_by_msg: dict[str, list[dict[str, Any]]] = {mid: [] for mid in msg_ids}
            for call_row in call_rows:
                call_dict = dict(call_row)
                calls_by_msg[call_dict["message_id"]].append(call_dict)

            for msg in messages:
                msg["tool_calls"] = calls_by_msg.get(msg["id"], [])
            return messages

    async def list_messages(
        self, session_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """List recent messages for a session in chronological order with tool_calls."""
        return await asyncio.to_thread(self._list_messages_sync, session_id, limit)

    def _add_tool_call_sync(
        self,
        message_id: str,
        server_name: str,
        tool_name: str,
        arguments_json: str | dict[str, Any],
        status: str,
        call_id: str | None,
    ) -> dict[str, Any]:
        cid = call_id or str(uuid.uuid4())
        args_str = (
            arguments_json
            if isinstance(arguments_json, str)
            else json.dumps(arguments_json)
        )
        now = _utc_now_iso()
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT INTO tool_calls (
                    id, message_id, server_name, tool_name,
                    arguments_json, result_summary, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, '', ?, ?)
                """,
                (cid, message_id, server_name, tool_name, args_str, status, now),
            )
            conn.commit()
        return {
            "id": cid,
            "message_id": message_id,
            "server_name": server_name,
            "tool_name": tool_name,
            "arguments_json": args_str,
            "result_summary": "",
            "status": status,
            "created_at": now,
        }

    async def add_tool_call(
        self,
        message_id: str,
        server_name: str,
        tool_name: str,
        arguments_json: str | dict[str, Any],
        status: str = "running",
        call_id: str | None = None,
    ) -> dict[str, Any]:
        """Record an MCP tool invocation linked to a message."""
        return await asyncio.to_thread(
            self._add_tool_call_sync,
            message_id,
            server_name,
            tool_name,
            arguments_json,
            status,
            call_id,
        )

    def _update_tool_call_sync(
        self, call_id: str, status: str, result_summary: str
    ) -> dict[str, Any] | None:
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute(
                "UPDATE tool_calls SET status = ?, result_summary = ? WHERE id = ?",
                (status, result_summary, call_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                """
                SELECT id, message_id, server_name, tool_name, arguments_json,
                       result_summary, status, created_at
                FROM tool_calls
                WHERE id = ?
                """,
                (call_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    async def update_tool_call(
        self, call_id: str, status: str, result_summary: str = ""
    ) -> dict[str, Any] | None:
        """Update execution status and summary for a recorded tool call."""
        return await asyncio.to_thread(
            self._update_tool_call_sync, call_id, status, result_summary
        )

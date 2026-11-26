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
                    workspace_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    input_mode TEXT NOT NULL DEFAULT 'text',
                    content TEXT NOT NULL,
                    parts_json TEXT NOT NULL DEFAULT '[]',
                    emotion TEXT NOT NULL DEFAULT 'neutral',
                    usage_json TEXT,
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
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "workspace_path" not in cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN workspace_path TEXT")
            msg_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "parts_json" not in msg_cols:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN parts_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "usage_json" not in msg_cols:
                conn.execute("ALTER TABLE messages ADD COLUMN usage_json TEXT")
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
            "workspace_path": None,
            "created_at": now,
            "updated_at": now,
        }

    def _get_or_create_active_session_sync(self) -> dict[str, Any]:
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                """
                SELECT id, title, workspace_path, created_at, updated_at
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

    def _get_session_sync(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                """
                SELECT id, title, workspace_path, created_at, updated_at
                FROM sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Return a single session record by id, or None if not found."""
        return await asyncio.to_thread(self._get_session_sync, session_id)

    def _rename_session_sync(self, session_id: str, title: str) -> dict[str, Any] | None:
        clean_title = (title or "").strip()[:80].strip() or "New Chat"
        now = _utc_now_iso()
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (clean_title, now, session_id),
            )
            if cur.rowcount == 0:
                conn.commit()
                return None
            conn.commit()
            row = conn.execute(
                """
                SELECT id, title, workspace_path, created_at, updated_at
                FROM sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    async def rename_session(self, session_id: str, title: str) -> dict[str, Any] | None:
        """Rename a session (capped to 80 chars, defaulting to 'New Chat' if empty)."""
        return await asyncio.to_thread(self._rename_session_sync, session_id, title)

    def _set_session_workspace_sync(
        self, session_id: str, workspace_path: str | None
    ) -> dict[str, Any] | None:
        cleaned = str(workspace_path).strip() if workspace_path is not None else None
        if cleaned == "":
            cleaned = None
        now = _utc_now_iso()
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute(
                "UPDATE sessions SET workspace_path = ?, updated_at = ? WHERE id = ?",
                (cleaned, now, session_id),
            )
            if cur.rowcount == 0:
                conn.commit()
                return None
            conn.commit()
            row = conn.execute(
                """
                SELECT id, title, workspace_path, created_at, updated_at
                FROM sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    async def set_session_workspace(
        self, session_id: str, workspace_path: str | None
    ) -> dict[str, Any] | None:
        """Bind or clear the workspace_path associated with a session."""
        return await asyncio.to_thread(
            self._set_session_workspace_sync, session_id, workspace_path
        )

    def _list_sessions_sync(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                """
                SELECT
                    s.id,
                    s.title,
                    s.workspace_path,
                    s.created_at,
                    COALESCE(MAX(m.created_at), s.created_at) AS updated_at,
                    COUNT(m.id) AS message_count,
                    (
                        SELECT m2.content
                        FROM messages m2
                        WHERE m2.session_id = s.id
                          AND m2.role IN ('user', 'assistant')
                          AND TRIM(m2.content) != ''
                        ORDER BY m2.created_at DESC, m2.rowid DESC
                        LIMIT 1
                    ) AS last_content
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.id
                GROUP BY s.id
                ORDER BY updated_at DESC, s.rowid DESC
                """
            ).fetchall()
            sessions: list[dict[str, Any]] = []
            for row in rows:
                raw_preview = row["last_content"]
                preview = " ".join(str(raw_preview).split()) if raw_preview else ""
                if len(preview) > 80:
                    preview = preview[:77].rstrip() + "..."
                sessions.append(
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "workspace_path": row["workspace_path"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "message_count": int(row["message_count"] or 0),
                        "preview": preview,
                    }
                )
            return sessions

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

    def _delete_message_sync(self, session_id: str, message_id: str) -> bool:
        with self._lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM tool_calls WHERE message_id = ?", (message_id,))
            cur = conn.execute(
                "DELETE FROM messages WHERE id = ? AND session_id = ?",
                (message_id, session_id),
            )
            conn.commit()
            return cur.rowcount > 0

    async def delete_message(self, session_id: str, message_id: str) -> bool:
        """Delete a single message and its associated tool calls from a session."""
        return await asyncio.to_thread(self._delete_message_sync, session_id, message_id)

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

    @staticmethod
    def _normalize_parts(parts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in parts or []:
            if not isinstance(item, dict):
                continue
            ptype = str(item.get("type") or "")
            if ptype == "text":
                text = str(item.get("text") or "")
                if text:
                    normalized.append({"type": "text", "text": text})
            elif ptype == "tool_call":
                call_id = str(item.get("call_id") or "").strip()
                if call_id:
                    normalized.append({"type": "tool_call", "call_id": call_id})
            elif ptype == "attachment":
                name = str(item.get("name") or "").strip()
                if name:
                    normalized.append(
                        {
                            "type": "attachment",
                            "name": name,
                            "mime_type": str(item.get("mime_type") or "application/octet-stream"),
                            "size": int(item.get("size") or 0),
                            "stored_path": str(item.get("stored_path") or ""),
                        }
                    )
        return normalized

    @classmethod
    def _serialize_parts(cls, parts: list[dict[str, Any]] | None) -> str:
        return json.dumps(cls._normalize_parts(parts), ensure_ascii=False)

    @classmethod
    def _deserialize_parts(
        cls,
        raw_parts_json: Any,
        content: str,
        tool_calls: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        parsed: list[dict[str, Any]] = []
        if isinstance(raw_parts_json, str) and raw_parts_json.strip():
            try:
                loaded = json.loads(raw_parts_json)
                if isinstance(loaded, list):
                    parsed = cls._normalize_parts(loaded)
            except json.JSONDecodeError:
                parsed = []
        if parsed:
            return parsed
        fallback: list[dict[str, Any]] = []
        if content:
            fallback.append({"type": "text", "text": content})
        for tc in tool_calls:
            cid = str(tc.get("id") or "").strip()
            if cid:
                fallback.append({"type": "tool_call", "call_id": cid})
        return fallback

    def _serialize_usage(self, usage: dict[str, Any] | None) -> str | None:
        if not isinstance(usage, dict):
            return None
        cleaned: dict[str, int] = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            val = usage.get(key)
            if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                cleaned[key] = val
        if not cleaned:
            return None
        return json.dumps(cleaned, ensure_ascii=False)

    def _deserialize_usage(self, usage_json: str | None) -> dict[str, int] | None:
        if not usage_json:
            return None
        try:
            raw = json.loads(usage_json)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        cleaned: dict[str, int] = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            val = raw.get(key)
            if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                cleaned[key] = val
        return cleaned or None

    def _add_message_sync(
        self,
        session_id: str,
        role: str,
        content: str,
        input_mode: str,
        emotion: str,
        message_id: str | None,
        parts: list[dict[str, Any]] | None,
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        mid = message_id or str(uuid.uuid4())
        now = _utc_now_iso()
        norm_parts = self._normalize_parts(parts)
        parts_json = json.dumps(norm_parts, ensure_ascii=False)
        usage_json = self._serialize_usage(usage)
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT INTO messages (id, session_id, role, input_mode, content, parts_json, emotion, usage_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (mid, session_id, role, input_mode, content, parts_json, emotion, usage_json, now),
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
            "parts": self._deserialize_parts(parts_json, content, []),
            "emotion": emotion,
            "usage": self._deserialize_usage(usage_json),
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
        parts: list[dict[str, Any]] | None = None,
        usage: dict[str, Any] | None = None,
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
            parts,
            usage,
        )

    def _update_message_sync(
        self,
        message_id: str,
        content: str,
        emotion: str | None,
        parts: list[dict[str, Any]] | None,
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        now = _utc_now_iso()
        parts_json = self._serialize_parts(parts) if parts is not None else None
        usage_json = self._serialize_usage(usage) if usage is not None else None
        set_clauses = ["content = ?"]
        params: list[Any] = [content]
        if emotion is not None:
            set_clauses.append("emotion = ?")
            params.append(emotion)
        if parts_json is not None:
            set_clauses.append("parts_json = ?")
            params.append(parts_json)
        if usage_json is not None:
            set_clauses.append("usage_json = ?")
            params.append(usage_json)
        params.append(message_id)
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute(
                f"UPDATE messages SET {', '.join(set_clauses)} WHERE id = ?",
                tuple(params),
            )
            if cur.rowcount == 0:
                conn.commit()
                return None

            row = conn.execute(
                """
                SELECT id, session_id, role, input_mode, content, parts_json, emotion, usage_json, created_at
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
            msg_calls = [dict(c) for c in calls]
            msg["tool_calls"] = msg_calls
            msg["usage"] = self._deserialize_usage(msg.pop("usage_json", None))
            msg["parts"] = self._deserialize_parts(
                msg.pop("parts_json", None), msg["content"], msg_calls
            )
            return msg

    async def update_message(
        self,
        message_id: str,
        content: str,
        emotion: str | None = None,
        parts: list[dict[str, Any]] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Update message content and optionally emotion, ordered parts, and token usage."""
        return await asyncio.to_thread(
            self._update_message_sync, message_id, content, emotion, parts, usage
        )

    def _list_messages_sync(self, session_id: str, limit: int | None) -> list[dict[str, Any]]:
        if limit is not None and limit <= 0:
            return []
        with self._lock:
            conn = self._get_conn()
            if limit is None:
                rows = conn.execute(
                    """
                    SELECT id, session_id, role, input_mode, content, parts_json, emotion, usage_json, created_at
                    FROM messages
                    WHERE session_id = ?
                    ORDER BY created_at ASC, rowid ASC
                    """,
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, session_id, role, input_mode, content, parts_json, emotion, usage_json, created_at
                    FROM (
                        SELECT id, session_id, role, input_mode, content, parts_json, emotion,
                               usage_json, created_at, rowid AS _rid
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
                msg_calls = calls_by_msg.get(msg["id"], [])
                msg["tool_calls"] = msg_calls
                msg["usage"] = self._deserialize_usage(msg.pop("usage_json", None))
                msg["parts"] = self._deserialize_parts(
                    msg.pop("parts_json", None), msg["content"], msg_calls
                )
            return messages

    async def list_messages(
        self, session_id: str, limit: int | None = 100
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

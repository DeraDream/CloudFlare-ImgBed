# -*- coding: utf-8 -*-
"""SQLite upload history for the Telegram upload client.

This module intentionally keeps Bot-only metadata (history, retry source IDs,
SHA-256 dedupe keys, tags/notes) in telegram-bot-data/bot.db. It does not need
extra ImgBed management permissions beyond the existing upload token.
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_history_db(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS upload_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER,
                media_group_id TEXT,
                source_file_id TEXT,
                source_unique_id TEXT,
                original_name TEXT,
                final_name TEXT,
                mime_type TEXT,
                original_size INTEGER DEFAULT 0,
                final_size INTEGER DEFAULT 0,
                width INTEGER DEFAULT 0,
                height INTEGER DEFAULT 0,
                duration INTEGER DEFAULT 0,
                channel_type TEXT,
                channel_name TEXT,
                upload_folder TEXT,
                url TEXT,
                sha256 TEXT,
                tags TEXT NOT NULL DEFAULT '[]',
                note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '',
                is_duplicate INTEGER NOT NULL DEFAULT 0,
                settings_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tg_history_user_time "
            "ON upload_history(user_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tg_history_sha "
            "ON upload_history(sha256, channel_type, channel_name, upload_folder, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tg_history_group "
            "ON upload_history(media_group_id, created_at)"
        )


def _encode(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return int(value)
    return value


def create_history(db_path: str, values: Dict[str, Any]) -> int:
    now = int(time.time())
    payload = dict(values)
    payload.setdefault("created_at", now)
    payload.setdefault("updated_at", now)
    payload.setdefault("tags", [])
    payload.setdefault("settings_json", {})
    keys = list(payload.keys())
    placeholders = ",".join("?" for _ in keys)
    with connect(db_path) as conn:
        cur = conn.execute(
            f"INSERT INTO upload_history ({','.join(keys)}) VALUES ({placeholders})",
            [_encode(payload[k]) for k in keys],
        )
        return int(cur.lastrowid)


def update_history(db_path: str, history_id: int, **values: Any) -> None:
    if not values:
        return
    payload = dict(values)
    payload["updated_at"] = int(time.time())
    keys = list(payload.keys())
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE upload_history SET "
            + ", ".join(f"{key}=?" for key in keys)
            + " WHERE id=?",
            [_encode(payload[k]) for k in keys] + [int(history_id)],
        )


def get_history(db_path: str, history_id: int, user_id: Optional[int] = None):
    sql = "SELECT * FROM upload_history WHERE id=?"
    params: List[Any] = [int(history_id)]
    if user_id is not None:
        sql += " AND user_id=?"
        params.append(int(user_id))
    with connect(db_path) as conn:
        return conn.execute(sql, params).fetchone()


def list_history(
    db_path: str,
    user_id: int,
    *,
    limit: int = 5,
    offset: int = 0,
):
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM upload_history
            WHERE user_id=?
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (int(user_id), int(limit), int(offset)),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM upload_history WHERE user_id=?",
            (int(user_id),),
        ).fetchone()["c"]
    return rows, int(total)


def find_duplicate(
    db_path: str,
    *,
    sha256: str,
    channel_type: str,
    channel_name: str,
    upload_folder: str,
    exclude_id: Optional[int] = None,
):
    sql = """
        SELECT * FROM upload_history
        WHERE sha256=?
          AND channel_type=?
          AND channel_name=?
          AND upload_folder=?
          AND status='success'
          AND url IS NOT NULL
          AND url != ''
    """
    params: List[Any] = [sha256, channel_type, channel_name, upload_folder]
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(int(exclude_id))
    sql += " ORDER BY updated_at DESC, id DESC LIMIT 1"
    with connect(db_path) as conn:
        return conn.execute(sql, params).fetchone()


def update_tags_note(
    db_path: str,
    history_id: int,
    user_id: int,
    tags: List[str],
    note: str,
) -> bool:
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE upload_history
            SET tags=?, note=?, updated_at=?
            WHERE id=? AND user_id=?
            """,
            (
                json.dumps(tags, ensure_ascii=False),
                note,
                int(time.time()),
                int(history_id),
                int(user_id),
            ),
        )
        return cur.rowcount > 0


def decode_tags(row) -> List[str]:
    if row is None:
        return []
    raw = row["tags"] if "tags" in row.keys() else "[]"
    try:
        tags = json.loads(raw or "[]")
        if isinstance(tags, list):
            return [str(x) for x in tags if str(x).strip()]
    except Exception:
        pass
    return []


def decode_settings(row) -> Dict[str, Any]:
    if row is None:
        return {}
    raw = row["settings_json"] if "settings_json" in row.keys() else "{}"
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}

"""历史记录存储（本地 SQLite）。

隐私默认值：**不保存 JSON 原文**，只存标签、路径、时间与元数据；
用户可以在设置里显式打开"保存原文"。所有数据都留在本机，绝不联网。
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.settings.settings import settings
from app.utils.resources import user_data_dir

KIND_FILE = "file"
KIND_FORMAT = "format"
KIND_QUERY = "query"
KIND_SCHEMA = "schema"
KINDS = (KIND_FILE, KIND_FORMAT, KIND_QUERY, KIND_SCHEMA)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '',
    path        TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL DEFAULT '',
    meta        TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_kind_created ON history(kind, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_created ON history(created_at DESC);
"""

__all__ = ["HistoryEntry", "HistoryStore", "KINDS", "KIND_FILE", "KIND_FORMAT",
           "KIND_QUERY", "KIND_SCHEMA", "default_db_path"]


def default_db_path() -> str:
    return os.path.join(user_data_dir(), "history.sqlite")


@dataclass
class HistoryEntry:
    id: int
    kind: str
    label: str
    detail: str
    path: str
    content: str
    created_at: str
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_content(self) -> bool:
        return bool(self.content)

    def display_time(self) -> str:
        try:
            return datetime.fromisoformat(self.created_at).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return self.created_at


class HistoryStore:
    """历史记录读写。所有方法都不抛异常给 UI 层（失败返回 False / 空列表）。"""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or default_db_path()
        self._connection: Optional[sqlite3.Connection] = None
        self._broken = ""
        self._connect()

    # ------------------------------------------------------------------ 连接
    def _connect(self) -> None:
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            self._connection = sqlite3.connect(self.path)
            self._connection.row_factory = sqlite3.Row
            self._connection.executescript(_SCHEMA)
            self._connection.commit()
        except sqlite3.Error as exc:
            self._connection = None
            self._broken = f"{type(exc).__name__}: {exc}"
        except OSError as exc:
            self._connection = None
            self._broken = f"{type(exc).__name__}: {exc}"

    @property
    def available(self) -> bool:
        return self._connection is not None

    @property
    def error(self) -> str:
        return self._broken

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except sqlite3.Error:
                pass
            self._connection = None

    # ------------------------------------------------------------------ 写入
    def record(self, kind: str, label: str, *, detail: str = "", path: str = "",
               content: str = "", meta: Optional[Dict[str, Any]] = None) -> Optional[int]:
        """写一条记录；受隐私设置约束。返回记录 id，未记录时返回 None。"""
        if not settings.history_enabled or self._connection is None:
            return None
        if kind not in KINDS:
            return None
        stored_content = content if settings.history_store_content else ""
        if stored_content and len(stored_content) > 2_000_000:
            # 原文太大就不存，避免数据库膨胀
            stored_content = ""
            meta = dict(meta or {})
            meta["content_omitted_too_large"] = True
        try:
            cursor = self._connection.execute(
                "INSERT INTO history (kind, label, detail, path, content, meta, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (kind, label, detail, path, stored_content,
                 json.dumps(meta or {}, ensure_ascii=False),
                 datetime.now().isoformat(timespec="seconds")),
            )
            self._connection.commit()
            self._trim(settings.history_limit)
            return int(cursor.lastrowid)
        except sqlite3.Error:
            return None

    def _trim(self, limit: int) -> None:
        limit = max(10, int(limit))
        try:
            self._connection.execute(
                "DELETE FROM history WHERE id NOT IN "
                "(SELECT id FROM history ORDER BY created_at DESC, id DESC LIMIT ?)", (limit,))
            self._connection.commit()
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------------ 读取
    def list(self, kind: Optional[str] = None, limit: int = 200,
             offset: int = 0) -> List[HistoryEntry]:
        if self._connection is None:
            return []
        query = "SELECT * FROM history"
        params: List[Any] = []
        if kind:
            query += " WHERE kind = ?"
            params.append(kind)
        query += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([max(1, int(limit)), max(0, int(offset))])
        try:
            rows = self._connection.execute(query, params).fetchall()
        except sqlite3.Error:
            return []
        return [self._to_entry(row) for row in rows]

    def get(self, entry_id: int) -> Optional[HistoryEntry]:
        if self._connection is None:
            return None
        try:
            row = self._connection.execute(
                "SELECT * FROM history WHERE id = ?", (int(entry_id),)).fetchone()
        except (sqlite3.Error, ValueError):
            return None
        return self._to_entry(row) if row is not None else None

    @staticmethod
    def _to_entry(row: sqlite3.Row) -> HistoryEntry:
        try:
            meta = json.loads(row["meta"]) if row["meta"] else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        return HistoryEntry(
            id=int(row["id"]),
            kind=str(row["kind"]),
            label=str(row["label"] or ""),
            detail=str(row["detail"] or ""),
            path=str(row["path"] or ""),
            content=str(row["content"] or ""),
            created_at=str(row["created_at"] or ""),
            meta=meta if isinstance(meta, dict) else {},
        )

    # ------------------------------------------------------------------ 删除
    def delete(self, entry_id: int) -> bool:
        if self._connection is None:
            return False
        try:
            cursor = self._connection.execute("DELETE FROM history WHERE id = ?", (int(entry_id),))
            self._connection.commit()
            return cursor.rowcount > 0
        except (sqlite3.Error, ValueError):
            return False

    def clear_kind(self, kind: str) -> int:
        if self._connection is None:
            return 0
        try:
            cursor = self._connection.execute("DELETE FROM history WHERE kind = ?", (kind,))
            self._connection.commit()
            return max(0, cursor.rowcount)
        except sqlite3.Error:
            return 0

    def clear_all(self) -> int:
        """清空全部历史（设置里的"一键清空本地数据"）。"""
        if self._connection is None:
            return 0
        try:
            count = self.count()
            self._connection.execute("DELETE FROM history")
            self._connection.execute("DELETE FROM sqlite_sequence WHERE name = 'history'")
            self._connection.commit()
            self._connection.execute("VACUUM")
            return count
        except sqlite3.Error:
            return 0

    def count(self, kind: Optional[str] = None) -> int:
        if self._connection is None:
            return 0
        query = "SELECT COUNT(*) FROM history"
        params: List[Any] = []
        if kind:
            query += " WHERE kind = ?"
            params.append(kind)
        try:
            row = self._connection.execute(query, params).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0

    def distinct_labels(self, kind: str, limit: int = 20) -> List[str]:
        """最近的去重标签，用于"最近使用的 JSONPath / Schema"下拉。"""
        if self._connection is None:
            return []
        try:
            rows = self._connection.execute(
                "SELECT label FROM history WHERE kind = ? AND label <> '' "
                "GROUP BY label ORDER BY MAX(created_at) DESC LIMIT ?",
                (kind, max(1, int(limit)))).fetchall()
        except sqlite3.Error:
            return []
        return [str(row["label"]) for row in rows]

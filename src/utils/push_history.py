"""
推送记录明细：每条事件推送写入 SQLite，最多保留 1 万条，超出时删除最老的 3000 条。
供 Web 推送汇总「查看详细」使用。
"""

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
from zoneinfo import ZoneInfo

_lock = threading.Lock()
_db_path: str = ""
MAX_RECORDS = 10000
DELETE_BATCH = 3000


def init(cursor_dir: str) -> None:
    """初始化数据库路径并建表。cursor_dir 与 push_stats 一致。"""
    global _db_path
    if not cursor_dir:
        cursor_dir = "./data/cursor"
    abs_dir = os.path.abspath(os.path.join(os.getcwd(), cursor_dir))
    Path(abs_dir).mkdir(parents=True, exist_ok=True)
    _db_path = os.path.join(abs_dir, "push_history.db")
    _ensure_table()


def _ensure_table() -> None:
    if not _db_path:
        return
    with _lock:
        conn = sqlite3.connect(_db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS push_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    detail TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_push_history_created_at ON push_history(created_at)"
            )
            conn.commit()
        finally:
            conn.close()


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(_db_path)


def add_record(
    success: bool,
    event_type: str,
    summary: str = "",
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    """写入一条推送记录。若总条数超过 MAX_RECORDS，删除最老的 DELETE_BATCH 条。"""
    if not _db_path:
        return
    created_at = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    detail_str = json.dumps(detail, ensure_ascii=False) if detail else None
    if detail_str and len(detail_str) > 5000:
        # 截断可能导致无效 JSON，前端需容错（如 try/catch JSON.parse）
        detail_str = detail_str[:4997] + "..."
    if summary and len(summary) > 500:
        summary = summary[:497] + "..."
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO push_history (created_at, event_type, success, summary, detail) VALUES (?, ?, ?, ?, ?)",
                (created_at, event_type, 1 if success else 0, summary or "", detail_str),
            )
            conn.commit()
            cur = conn.execute("SELECT COUNT(*) FROM push_history")
            count = cur.fetchone()[0]
            if count > MAX_RECORDS:
                cur = conn.execute(
                    "SELECT id FROM push_history ORDER BY id ASC LIMIT ?", (DELETE_BATCH,)
                )
                ids = [row[0] for row in cur.fetchall()]
                if ids:
                    placeholders = ",".join("?" * len(ids))
                    conn.execute(f"DELETE FROM push_history WHERE id IN ({placeholders})", ids)
                    conn.commit()
        finally:
            conn.close()


def bulk_insert(records: List[Dict[str, Any]]) -> None:
    """批量插入推送记录（用于造数等）。每条为 dict：created_at, event_type, success, summary, detail(可选)。
    插入后若总条数超过 MAX_RECORDS，会删除最老的 DELETE_BATCH 条。"""
    if not _db_path or not records:
        return
    rows = []
    for r in records:
        created_at = str(r.get("created_at", ""))[:19]
        event_type = str(r.get("event_type", "")) or "Unknown"
        success = 1 if r.get("success", True) else 0
        summary = str(r.get("summary", ""))[:500]
        if len(str(r.get("summary", ""))) > 500:
            summary = summary[:497] + "..."
        detail = r.get("detail")
        detail_str = json.dumps(detail, ensure_ascii=False) if detail is not None else None
        if detail_str and len(detail_str) > 5000:
            detail_str = detail_str[:4997] + "..."  # 截断后可能非合法 JSON，前端需容错
        rows.append((created_at, event_type, success, summary or "", detail_str))
    with _lock:
        conn = _conn()
        try:
            conn.executemany(
                "INSERT INTO push_history (created_at, event_type, success, summary, detail) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
            cur = conn.execute("SELECT COUNT(*) FROM push_history")
            count = cur.fetchone()[0]
            if count > MAX_RECORDS:
                cur = conn.execute(
                    "SELECT id FROM push_history ORDER BY id ASC LIMIT ?", (DELETE_BATCH,)
                )
                ids = [row[0] for row in cur.fetchall()]
                if ids:
                    placeholders = ",".join("?" * len(ids))
                    conn.execute(f"DELETE FROM push_history WHERE id IN ({placeholders})", ids)
                    conn.commit()
        finally:
            conn.close()


def get_records(
    limit: int = 50,
    offset: int = 0,
    success_filter: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """分页查询推送记录，按时间倒序。success_filter: True 仅成功，False 仅失败，None 全部。"""
    if not _db_path:
        return []
    with _lock:
        conn = _conn()
        conn.row_factory = sqlite3.Row
        try:
            sql = "SELECT id, created_at, event_type, success, summary, detail FROM push_history"
            params: List[Any] = []
            if success_filter is not None:
                sql += " WHERE success = ?"
                params.append(1 if success_filter else 0)
            sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
            return [
                {
                    "id": r["id"],
                    "created_at": r["created_at"],
                    "event_type": r["event_type"],
                    "success": bool(r["success"]),
                    "summary": r["summary"] or "",
                    "detail": r["detail"],
                }
                for r in rows
            ]
        finally:
            conn.close()


def get_record(record_id: int) -> Optional[Dict[str, Any]]:
    """根据 id 查询单条推送记录。"""
    if not _db_path:
        return None
    with _lock:
        conn = _conn()
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "SELECT id, created_at, event_type, success, summary, detail FROM push_history WHERE id = ?",
                (record_id,),
            )
            r = cur.fetchone()
            if r is None:
                return None
            return {
                "id": r["id"],
                "created_at": r["created_at"],
                "event_type": r["event_type"],
                "success": bool(r["success"]),
                "summary": r["summary"] or "",
                "detail": r["detail"],
            }
        finally:
            conn.close()


def get_total_counts() -> Dict[str, int]:
    """返回总统计：total, success, fail（基于 SQLite push_history）。"""
    if not _db_path:
        return {"total": 0, "success": 0, "fail": 0}
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS ok FROM push_history")
            row = cur.fetchone()
            total = row[0] or 0
            ok = row[1] or 0
            fail = total - ok
            return {"total": total, "success": ok, "fail": fail}
        finally:
            conn.close()


def get_today_counts() -> Dict[str, int]:
    """返回当日统计：total, success, fail（Asia/Shanghai，当天字符串匹配）。"""
    if not _db_path:
        return {"total": 0, "success": 0, "fail": 0}
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS ok "
                "FROM push_history WHERE substr(created_at, 1, 10) = ?",
                (today,),
            )
            row = cur.fetchone()
            total = row[0] or 0
            ok = row[1] or 0
            fail = total - ok
            return {"total": total, "success": ok, "fail": fail}
        finally:
            conn.close()


def clear_all() -> None:
    """清空所有推送记录（仅用于造数脚本重新生成等）。"""
    if not _db_path:
        return
    with _lock:
        conn = _conn()
        try:
            conn.execute("DELETE FROM push_history")
            conn.commit()
        finally:
            conn.close()


def get_db_path() -> str:
    return _db_path

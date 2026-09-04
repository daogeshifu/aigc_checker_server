from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TASK_LOG_DB_PATH = Path(os.getenv("TASK_LOG_DB_PATH", "data/task_logs.db"))
TASK_LOG_MAX_RECORDS = max(20, int(os.getenv("TASK_LOG_MAX_RECORDS", "200")))

_LOCK = threading.RLock()
_INITIALIZED = False
_SENSITIVE_KEYS = {
    "content",
    "text",
    "prompt",
    "user_prompt",
    "system_prompt",
    "messages",
    "original",
    "reduced",
    "revised",
}
_IDENTIFIER_KEYS = {"model", "target_model", "requested_model", "deployment", "requested_api_source"}
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,159}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _connect() -> sqlite3.Connection:
    TASK_LOG_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(TASK_LOG_DB_PATH, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _initialize() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return
    with _LOCK:
        if _INITIALIZED:
            return
        with _connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL DEFAULT 'unknown',
                    task_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    plan TEXT NOT NULL,
                    requested_api_source TEXT,
                    requested_model TEXT,
                    char_count INTEGER NOT NULL DEFAULT 0,
                    result_summary TEXT,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    duration_ms INTEGER
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    step TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    elapsed_ms INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
                    UNIQUE(task_id, sequence)
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_created_at
                    ON tasks(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_task_events_task_sequence
                    ON task_events(task_id, sequence);
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
            if "client_id" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN client_id TEXT NOT NULL DEFAULT 'unknown'")
        _INITIALIZED = True


def _safe_value(key: str, value: Any) -> Any:
    """Only persist operational metadata; user text and prompts are always removed."""
    if key.lower() in _SENSITIVE_KEYS:
        return "[内容已隐藏]"
    if key.lower() in _IDENTIFIER_KEYS and isinstance(value, str) and not _SAFE_IDENTIFIER_RE.fullmatch(value):
        return "[自定义值已隐藏]"
    if isinstance(value, BaseException):
        return value.__class__.__name__
    if isinstance(value, dict):
        return {str(k): _safe_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(key, item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value[:240] if isinstance(value, str) else value
    return str(value)[:240]


def _safe_details(details: dict[str, Any] | None) -> dict[str, Any]:
    return {str(key): _safe_value(str(key), value) for key, value in (details or {}).items()}


def _event_status(step: str, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    lowered = step.lower()
    if any(part in lowered for part in ("failure", "error", "rollback")):
        return "failed"
    if "skip" in lowered:
        return "skipped"
    if any(part in lowered for part in ("finish", "complete", "merge", "return")):
        return "success"
    if any(part in lowered for part in ("start", "call", "accept", "trigger")):
        return "running"
    return "info"


def start_task(
    task_id: str,
    client_id: str,
    task_type: str,
    source: str,
    plan: str,
    requested_api_source: str | None,
    requested_model: str | None,
    char_count: int,
) -> None:
    _initialize()
    now = _iso()
    normalized_source = source if source in {"demo", "test", "api"} else "api"
    with _LOCK, _connect() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO tasks (
                task_id, client_id, task_type, source, status, plan,
                requested_api_source, requested_model, char_count,
                result_summary, error_code, created_at, updated_at, duration_ms
            ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, NULL, NULL, ?, ?, NULL)
            """,
            (
                task_id,
                client_id,
                task_type,
                normalized_source,
                plan,
                _safe_value("requested_api_source", requested_api_source),
                _safe_value("requested_model", requested_model),
                char_count,
                now,
                now,
            ),
        )
        connection.execute(
            """
            DELETE FROM tasks
            WHERE task_id IN (
                SELECT task_id FROM tasks
                ORDER BY created_at DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (TASK_LOG_MAX_RECORDS,),
        )


def record_task_event(
    task_id: str | None,
    step: str,
    *,
    status: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    if not task_id:
        return
    _initialize()
    now = _now()
    with _LOCK, _connect() as connection:
        task = connection.execute(
            "SELECT created_at FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if not task:
            return
        created_at = datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
        elapsed_ms = max(0, int((now - created_at).total_seconds() * 1000))
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_events WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO task_events (
                task_id, sequence, step, status, details, created_at, elapsed_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                sequence,
                step,
                _event_status(step, status),
                json.dumps(_safe_details(details), ensure_ascii=False, separators=(",", ":")),
                _iso(now),
                elapsed_ms,
            ),
        )
        connection.execute(
            "UPDATE tasks SET updated_at = ? WHERE task_id = ?",
            (_iso(now), task_id),
        )


def finish_task(task_id: str, result_summary: dict[str, Any]) -> None:
    _finish_task(task_id, status="success", result_summary=result_summary, error_code=None)


def fail_task(task_id: str, error_code: str) -> None:
    _finish_task(task_id, status="failed", result_summary=None, error_code=error_code)


def _finish_task(
    task_id: str,
    *,
    status: str,
    result_summary: dict[str, Any] | None,
    error_code: str | None,
) -> None:
    _initialize()
    now = _now()
    with _LOCK, _connect() as connection:
        task = connection.execute(
            "SELECT created_at FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if not task:
            return
        created_at = datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
        duration_ms = max(0, int((now - created_at).total_seconds() * 1000))
        connection.execute(
            """
            UPDATE tasks
            SET status = ?, result_summary = ?, error_code = ?, updated_at = ?, duration_ms = ?
            WHERE task_id = ?
            """,
            (
                status,
                json.dumps(_safe_details(result_summary), ensure_ascii=False, separators=(",", ":"))
                if result_summary is not None
                else None,
                error_code,
                _iso(now),
                duration_ms,
                task_id,
            ),
        )


def _task_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item.pop("client_id", None)
    raw_summary = item.pop("result_summary", None)
    item["result"] = json.loads(raw_summary) if raw_summary else None
    return item


def list_tasks(client_id: str, limit: int = 30) -> list[dict[str, Any]]:
    _initialize()
    safe_limit = max(1, min(limit, 100))
    with _LOCK, _connect() as connection:
        rows = connection.execute(
            """
            SELECT task_id, task_type, source, status, plan,
                   requested_api_source, requested_model, char_count,
                   result_summary, error_code, created_at, updated_at, duration_ms
            FROM tasks
            WHERE client_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (client_id, safe_limit),
        ).fetchall()
    return [_task_row(row) for row in rows]


def get_task(task_id: str, client_id: str) -> dict[str, Any] | None:
    _initialize()
    with _LOCK, _connect() as connection:
        task = connection.execute(
            "SELECT * FROM tasks WHERE task_id = ? AND client_id = ?",
            (task_id, client_id),
        ).fetchone()
        if not task:
            return None
        events = connection.execute(
            """
            SELECT sequence, step, status, details, created_at, elapsed_ms
            FROM task_events
            WHERE task_id = ?
            ORDER BY sequence ASC
            """,
            (task_id,),
        ).fetchall()

    result = _task_row(task)
    result["events"] = [
        {
            **{key: event[key] for key in ("sequence", "step", "status", "created_at", "elapsed_ms")},
            "details": json.loads(event["details"]),
        }
        for event in events
    ]
    return result

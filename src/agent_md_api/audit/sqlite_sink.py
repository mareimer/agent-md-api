"""SQLite-Adapter — append-only, WAL-Modus, einziger Adapter mit `query()` (spec.md §9.3/§9.4)."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from agent_md_api.audit.sink import AuditSink
from agent_md_api.domain.models import AuditLogEntry, AuditLogResponse, AuditQueryFilter

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    user_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    path TEXT,
    kind TEXT,
    pii_accessed INTEGER NOT NULL,
    commit_id TEXT,
    reason TEXT,
    result TEXT NOT NULL,
    task_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_pii ON audit_log(pii_accessed);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp);
"""

_PAGE_SIZE = 200


class SqliteAuditSink(AuditSink):
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def record(self, entry: AuditLogEntry) -> None:
        path = entry.path if isinstance(entry.path, str) else json.dumps(entry.path) if entry.path else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log (id, timestamp, user_id, client_id, operation, path, kind, "
                "pii_accessed, commit_id, reason, result, task_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry.id,
                    entry.timestamp,
                    entry.user_id,
                    entry.client_id,
                    entry.operation.value,
                    path,
                    entry.kind.value if entry.kind else None,
                    int(entry.pii_accessed),
                    entry.commit_id,
                    entry.reason,
                    entry.result.value,
                    entry.task_id,
                ),
            )
            self._conn.commit()

    def query(self, filters: AuditQueryFilter) -> AuditLogResponse:
        clauses: list[str] = []
        params: list[object] = []
        if filters.user_id:
            clauses.append("user_id = ?")
            params.append(filters.user_id)
        if filters.path_prefix:
            clauses.append("path LIKE ?")
            params.append(f"{filters.path_prefix}%")
        if filters.pii_only:
            clauses.append("pii_accessed = 1")
        if filters.operation:
            clauses.append("operation = ?")
            params.append(filters.operation.value)
        if filters.from_ts:
            clauses.append("timestamp >= ?")
            params.append(filters.from_ts)
        if filters.to_ts:
            clauses.append("timestamp <= ?")
            params.append(filters.to_ts)
        if filters.cursor:
            clauses.append("id > ?")
            params.append(filters.cursor)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM audit_log {where} ORDER BY id LIMIT ?"  # noqa: S608 - Spalten fix, Werte parametrisiert
        params.append(_PAGE_SIZE + 1)

        with self._lock:
            cursor = self._conn.execute(sql, params)
            rows = cursor.fetchall()
            cols = [d[0] for d in cursor.description] if cursor.description else []

        has_more = len(rows) > _PAGE_SIZE
        rows = rows[:_PAGE_SIZE]
        entries = [self._row_to_entry(dict(zip(cols, row, strict=True))) for row in rows]
        next_cursor = entries[-1].id if has_more and entries else None
        return AuditLogResponse(entries=entries, next_cursor=next_cursor)

    @staticmethod
    def _row_to_entry(row: dict) -> AuditLogEntry:
        path = row["path"]
        if path and path.startswith("["):
            path = json.loads(path)
        return AuditLogEntry(
            id=row["id"],
            timestamp=row["timestamp"],
            user_id=row["user_id"],
            client_id=row["client_id"],
            operation=row["operation"],
            path=path,
            kind=row["kind"],
            pii_accessed=bool(row["pii_accessed"]),
            commit_id=row["commit_id"],
            reason=row["reason"],
            result=row["result"],
            task_id=row["task_id"],
        )

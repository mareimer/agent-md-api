"""Wählt den Audit-Adapter anhand der Konfiguration (spec.md §9.3)."""

from __future__ import annotations

from agent_md_api.audit.jsonl_sink import JsonlAuditSink
from agent_md_api.audit.none_sink import NoneAuditSink
from agent_md_api.audit.sink import AuditSink
from agent_md_api.audit.sqlite_sink import SqliteAuditSink
from agent_md_api.config import AuditBackend, Settings


def build_audit_sink(settings: Settings) -> AuditSink:
    match settings.audit_backend:
        case AuditBackend.NONE:
            return NoneAuditSink()
        case AuditBackend.SQLITE:
            assert settings.audit_db_path is not None  # von Settings._check_audit_backend_config gesetzt
            return SqliteAuditSink(settings.audit_db_path)
        case AuditBackend.JSONL:
            assert settings.audit_log_path is not None
            return JsonlAuditSink(settings.audit_log_path)
        case _:
            # Settings._check_audit_backend_config lehnt loki/elk/azure/aws bereits beim Start ab —
            # dieser Zweig ist nur zur Vollständigkeit für den Type-Checker da.
            raise ValueError(f"Audit-Backend '{settings.audit_backend}' ist nicht implementiert.")

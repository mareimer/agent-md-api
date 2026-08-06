"""No-Op-Adapter — kein Overhead, aber auch kein Compliance-Nutzen. Nur für Dev/Test (spec.md §9.3)."""

from __future__ import annotations

from agent_md_api.audit.sink import AuditSink
from agent_md_api.domain.models import AuditLogEntry


class NoneAuditSink(AuditSink):
    def record(self, entry: AuditLogEntry) -> None:
        return None

"""AuditSink-Port (spec.md §9.3) — Port mit austauschbaren Adaptern.
Der Rest der App kennt nur `record()`/`query()`, nicht das konkrete Backend."""

from __future__ import annotations

from abc import ABC, abstractmethod

from agent_md_api.domain.errors import ValidationError
from agent_md_api.domain.models import AuditLogEntry, AuditLogResponse, AuditQueryFilter


class AuditSink(ABC):
    @abstractmethod
    def record(self, entry: AuditLogEntry) -> None: ...

    def query(self, filters: AuditQueryFilter) -> AuditLogResponse:
        """Default: nicht unterstützt (spec.md §9.4) — nur der sqlite-Adapter überschreibt das."""
        raise ValidationError(
            f"Abfrage nicht unterstützt bei Backend '{type(self).__name__}' — "
            "Auswertung über das jeweilige externe Tool (Grafana/Kibana/Azure Monitor/CloudWatch Insights)."
        )

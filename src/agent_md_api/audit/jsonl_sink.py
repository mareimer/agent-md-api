"""JSON-Lines-Adapter — deckt Loki/ELK/Azure/AWS über externe Standard-Shipper ab
(Promtail/Filebeat/Azure Monitor Agent/CloudWatch Agent), kein eigener Netzwerk-Client
(spec.md §9.3). `query()` bewusst nicht überschrieben — Auswertung läuft extern."""

from __future__ import annotations

import threading
from pathlib import Path

from agent_md_api.audit.sink import AuditSink
from agent_md_api.domain.models import AuditLogEntry


class JsonlAuditSink(AuditSink):
    def __init__(self, log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = log_path
        self._lock = threading.Lock()

    def record(self, entry: AuditLogEntry) -> None:
        line = entry.model_dump_json()
        with self._lock, self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

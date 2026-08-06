"""Audit-Middleware (spec.md §9.3): genau EIN `record()`-Aufruf pro Request, zentral hier —
nicht in einzelnen Handlern verstreut, damit kein neuer Endpunkt das Loggen vergisst.

Handler/Dependencies reichern über `request.state` an (optional, machen die Einträge
präziser, sind aber keine Voraussetzung dafür, dass überhaupt geloggt wird):
- `request.state.principal`: von der Auth-Dependency gesetzt (user_id/client_id).
- `request.state.audit_extra`: dict mit `path`, `kind`, `pii_accessed`, `commit_id`,
  `reason`, `task_id` — von den Endpoint-Handlern optional befüllt.

Fehlt beides, wird trotzdem ein Basis-Eintrag geschrieben (operation aus Methode+Pfad
abgeleitet, user_id/client_id `"unknown"`) — lieber ein grober Eintrag als gar keiner.
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import UTC, datetime

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from agent_md_api.domain.models import AuditLogEntry, AuditOperation, AuditResult

_ROUTE_TO_OPERATION: list[tuple[re.Pattern[str], str, AuditOperation]] = [
    (re.compile(r"^/api/v1/tree$"), "GET", AuditOperation.TREE),
    (re.compile(r"^/api/v1/file/.+/content$"), "GET", AuditOperation.READ_CONTENT),
    (re.compile(r"^/api/v1/file/.+/edit$"), "POST", AuditOperation.EDIT),
    (re.compile(r"^/api/v1/file/.+/append$"), "POST", AuditOperation.APPEND),
    (re.compile(r"^/api/v1/file/.+$"), "GET", AuditOperation.READ),
    (re.compile(r"^/api/v1/file/.+$"), "POST", AuditOperation.WRITE),
    (re.compile(r"^/api/v1/file/.+$"), "DELETE", AuditOperation.DELETE),
    (re.compile(r"^/api/v1/transaction$"), "POST", AuditOperation.TRANSACTION),
]


def _infer_operation(method: str, path: str) -> AuditOperation | None:
    for pattern, expected_method, operation in _ROUTE_TO_OPERATION:
        if method == expected_method and pattern.match(path):
            return operation
    return None


def _infer_result(status_code: int) -> AuditResult:
    if status_code < 400:
        return AuditResult.SUCCESS
    if status_code in (401, 403):
        return AuditResult.DENIED
    return AuditResult.ERROR


class AuditMiddleware(BaseHTTPMiddleware):
    """Liest den AuditSink lazy aus `request.app.state.audit_sink` statt ihn beim
    `add_middleware()`-Aufruf (Modul-Importzeit, vor dem Lifespan-Startup) fest zu binden —
    zu dem Zeitpunkt existiert der konfigurierte Sink noch nicht."""

    async def dispatch(self, request: Request, call_next) -> Response:  # noqa: ANN001
        request.state.principal = None
        request.state.audit_extra = {}

        started = time.monotonic()
        response = await call_next(request)
        duration_ms = (time.monotonic() - started) * 1000

        operation = request.state.audit_extra.get("operation") or _infer_operation(
            request.method, request.url.path
        )
        if operation is None:
            # System-/Admin-Endpunkte (/system/...) o.ä. — kein spec.md-§9-Fall, nicht loggen.
            return response

        principal = request.state.principal
        extra = request.state.audit_extra

        entry = AuditLogEntry(
            id=uuid.uuid4().hex,
            timestamp=datetime.now(UTC).isoformat(),
            user_id=principal.user_id if principal else "unknown",
            client_id=principal.client_id if principal else "unknown",
            operation=operation,
            path=extra.get("path"),
            kind=extra.get("kind"),
            pii_accessed=extra.get("pii_accessed", False),
            commit_id=extra.get("commit_id"),
            reason=extra.get("reason"),
            result=_infer_result(response.status_code),
            task_id=extra.get("task_id"),
        )
        request.app.state.audit_sink.record(entry)
        response.headers["X-Response-Time-Ms"] = f"{duration_ms:.1f}"
        return response

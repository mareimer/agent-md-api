"""GET /system/audit — Admin-only, nur bei Backends mit query()-Unterstützung (spec.md §9.4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from agent_md_api.api.dependencies import get_audit_sink, require_admin
from agent_md_api.audit.sink import AuditSink
from agent_md_api.domain.models import AuditLogResponse, AuditOperation, AuditQueryFilter

router = APIRouter(tags=["Audit"], dependencies=[Depends(require_admin)])


@router.get("/system/audit")
def query_audit(
    user_id: str | None = None,
    path_prefix: str | None = None,
    pii_only: bool = False,
    operation: AuditOperation | None = None,
    from_: str | None = None,
    to: str | None = None,
    cursor: str | None = None,
    sink: AuditSink = Depends(get_audit_sink),
) -> AuditLogResponse:
    filters = AuditQueryFilter(
        user_id=user_id, path_prefix=path_prefix, pii_only=pii_only, operation=operation,
        from_ts=from_, to_ts=to, cursor=cursor,
    )
    return sink.query(filters)

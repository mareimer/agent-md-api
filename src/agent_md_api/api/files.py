"""POST /file/{path}/edit, /append, POST/DELETE /file/{path} (spec.md §5).

Registrierungsreihenfolge wie in tree.py wichtig: /edit und /append sind
spezifischer als die allgemeine /file/{path}-Route und müssen zuerst stehen.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from agent_md_api.acl.engine import ACL_PATH, AclEngine
from agent_md_api.api.dependencies import get_acl_engine, get_principal, get_settings, get_storage
from agent_md_api.api.schemas import AppendRequest, DeleteRequest, EditRequest, FullWriteTextRequest
from agent_md_api.config import Settings
from agent_md_api.domain.errors import AclDeniedError, ValidationError
from agent_md_api.domain.models import (
    Kind,
    Principal,
    TransactionOperation,
    TransactionOperationType,
    WriteResult,
)
from agent_md_api.storage.git_repo import GitStorage, classify_kind

router = APIRouter(tags=["Files"])


def _require_write(acl: AclEngine, principal: Principal, path: str, kind: Kind, settings: Settings) -> None:
    """spec.md §8: /_system/acl.json ist NUR für den Admin schreibbar, unabhängig von
    allen sonstigen Pfad-/Kind-Regeln — bewusst als Sonderfall vor der normalen ACL geprüft."""
    if path == ACL_PATH:
        if principal.user_id != settings.admin_user_id:
            raise AclDeniedError(f"/{ACL_PATH} ist nur für {settings.admin_user_id} schreibbar.")
        return
    acl.require_write(principal=principal, path=path, kind=kind)


def _audit_extra(path: str, kind: Kind, reason: str, task_id: str | None, result: WriteResult) -> dict:
    return {
        "path": path,
        "kind": kind,
        "pii_accessed": kind is Kind.PII_JSON,
        "reason": reason,
        "task_id": task_id,
        "commit_id": result.commit_id,
    }


@router.post("/file/{path:path}/edit")
def edit_file(
    path: str,
    body: EditRequest,
    request: Request,
    storage: GitStorage = Depends(get_storage),
    acl: AclEngine = Depends(get_acl_engine),
    principal: Principal = Depends(get_principal),
    settings: Settings = Depends(get_settings),
) -> WriteResult:
    kind = classify_kind(path)
    _require_write(acl, principal, path, kind, settings)
    op = TransactionOperation(
        path=path, type=TransactionOperationType.EDIT, old_str=body.old_str, new_str=body.new_str, if_version=body.if_version
    )
    tx = storage.run_transaction([op], principal=principal, reason=body.reason, task_id=body.task_id)
    result = WriteResult(path=path, version=tx.files[0].version, commit_id=tx.commit_id)
    request.state.audit_extra = _audit_extra(path, kind, body.reason, body.task_id, result)
    return result


@router.post("/file/{path:path}/append")
def append_file(
    path: str,
    body: AppendRequest,
    request: Request,
    storage: GitStorage = Depends(get_storage),
    acl: AclEngine = Depends(get_acl_engine),
    principal: Principal = Depends(get_principal),
    settings: Settings = Depends(get_settings),
) -> WriteResult:
    kind = classify_kind(path)
    _require_write(acl, principal, path, kind, settings)
    op = TransactionOperation(path=path, type=TransactionOperationType.APPEND, content=body.content, if_version=body.if_version)
    tx = storage.run_transaction([op], principal=principal, reason=body.reason, task_id=body.task_id)
    result = WriteResult(path=path, version=tx.files[0].version, commit_id=tx.commit_id)
    request.state.audit_extra = _audit_extra(path, kind, body.reason, body.task_id, result)
    return result


@router.post("/file/{path:path}")
async def write_file(
    path: str,
    request: Request,
    storage: GitStorage = Depends(get_storage),
    acl: AclEngine = Depends(get_acl_engine),
    principal: Principal = Depends(get_principal),
    settings: Settings = Depends(get_settings),
) -> WriteResult:
    kind = classify_kind(path)
    _require_write(acl, principal, path, kind, settings)
    content_type = request.headers.get("content-type", "")

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None:
            raise ValidationError("multipart-Body ohne `file`-Feld.")
        data = await upload.read()  # type: ignore[union-attr]
        reason = str(form.get("reason", ""))
        if not reason:
            raise ValidationError("`reason` fehlt.")
        task_id = form.get("task_id")
        if_version = form.get("if_version") or None
        tx = storage.write_binary(
            path, data, if_version=if_version, principal=principal, reason=reason, task_id=str(task_id) if task_id else None
        )
    else:
        body = FullWriteTextRequest.model_validate(await request.json())
        op = TransactionOperation(path=path, type=TransactionOperationType.WRITE, content=body.content, if_version=body.if_version)
        tx = storage.run_transaction([op], principal=principal, reason=body.reason, task_id=body.task_id)
        reason, task_id = body.reason, body.task_id

    result = WriteResult(path=path, version=tx.files[0].version, commit_id=tx.commit_id)
    request.state.audit_extra = _audit_extra(path, kind, reason, task_id, result)
    return result


@router.delete("/file/{path:path}", status_code=204)
def delete_file(
    path: str,
    body: DeleteRequest,
    request: Request,
    storage: GitStorage = Depends(get_storage),
    acl: AclEngine = Depends(get_acl_engine),
    principal: Principal = Depends(get_principal),
    settings: Settings = Depends(get_settings),
) -> None:
    kind = classify_kind(path)
    _require_write(acl, principal, path, kind, settings)
    tx = storage.delete(path, if_version=body.if_version, principal=principal, reason=body.reason, task_id=body.task_id)
    request.state.audit_extra = {
        "path": path,
        "kind": kind,
        "pii_accessed": kind is Kind.PII_JSON,
        "reason": body.reason,
        "task_id": body.task_id,
        "commit_id": tx.commit_id,
    }

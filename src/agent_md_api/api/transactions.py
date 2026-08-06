"""POST /transaction (spec.md §6). ACL wird je Operation vorab geprüft — die Storage-Schicht
selbst kennt keine ACL (Trennung: Storage = Mechanik, ACL = Policy, spec.md §8)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from agent_md_api.acl.engine import ACL_PATH, AclEngine
from agent_md_api.api.dependencies import get_acl_engine, get_principal, get_settings, get_storage
from agent_md_api.api.schemas import TransactionRequest
from agent_md_api.config import Settings
from agent_md_api.domain.errors import AclDeniedError
from agent_md_api.domain.models import Kind, Principal, TransactionResult
from agent_md_api.storage.git_repo import GitStorage, classify_kind

router = APIRouter(tags=["Transactions"])


@router.post("/transaction")
def run_transaction(
    body: TransactionRequest,
    request: Request,
    storage: GitStorage = Depends(get_storage),
    acl: AclEngine = Depends(get_acl_engine),
    principal: Principal = Depends(get_principal),
    settings: Settings = Depends(get_settings),
) -> TransactionResult:
    for op in body.operations:
        kind = classify_kind(op.path)
        if op.path == ACL_PATH:
            if principal.user_id != settings.admin_user_id:
                raise AclDeniedError(f"/{ACL_PATH} ist nur für {settings.admin_user_id} schreibbar.")
        else:
            acl.require_write(principal=principal, path=op.path, kind=kind)

    result = storage.run_transaction(body.operations, principal=principal, reason=body.reason, task_id=body.task_id)
    request.state.audit_extra = {
        "path": [op.path for op in body.operations],
        "pii_accessed": any(classify_kind(op.path) is Kind.PII_JSON for op in body.operations),
        "reason": body.reason,
        "task_id": body.task_id,
        "commit_id": result.commit_id,
    }
    return result

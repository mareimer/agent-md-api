"""GET /tree, GET /file/{path}, GET /file/{path}/content (spec.md §4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from agent_md_api.acl.engine import AclEngine
from agent_md_api.api.dependencies import get_acl_engine, get_principal, get_storage
from agent_md_api.domain.models import FileResponse, Kind, Principal, TreeResponse
from agent_md_api.storage.git_repo import GitStorage, classify_kind

router = APIRouter(tags=["Tree"])


@router.get("/tree")
def get_tree(
    request: Request,
    root: str = "/",
    depth: int = 2,
    cursor: str | None = None,
    storage: GitStorage = Depends(get_storage),
    acl: AclEngine = Depends(get_acl_engine),
    principal: Principal = Depends(get_principal),
) -> TreeResponse:
    full = storage.list_tree(root, depth, cursor)
    visible = [
        e for e in full.entries if e.kind is Kind.DIR or acl.can_read(principal=principal, path=e.path, kind=e.kind)
    ]
    request.state.audit_extra = {"path": root or "/"}
    return TreeResponse(entries=visible, next_cursor=full.next_cursor)


# Reihenfolge wichtig: `{path:path}` ist bei Starlette greedy (matcht auch Schrägstriche) —
# die spezifischere .../content-Route MUSS vor der allgemeinen /file/{path:path}-Route
# registriert werden, sonst würde "foo/content" bereits von der allgemeinen Route als
# path="foo/content" verschluckt und /content nie erreicht.
@router.get("/file/{path:path}/content")
def get_file_content(
    path: str,
    request: Request,
    storage: GitStorage = Depends(get_storage),
    acl: AclEngine = Depends(get_acl_engine),
    principal: Principal = Depends(get_principal),
) -> Response:
    kind = classify_kind(path)
    acl.require_read(principal=principal, path=path, kind=kind)
    data, mime_type = storage.get_file_content_bytes(path)
    request.state.audit_extra = {"path": path, "kind": kind, "pii_accessed": False}  # binary != pii.json per Definition
    return Response(content=data, media_type=mime_type)


@router.get("/file/{path:path}")
def get_file(
    path: str,
    request: Request,
    response: Response,
    storage: GitStorage = Depends(get_storage),
    acl: AclEngine = Depends(get_acl_engine),
    principal: Principal = Depends(get_principal),
) -> FileResponse:
    file = storage.get_file(path)
    acl.require_read(principal=principal, path=path, kind=file.kind)
    response.headers["ETag"] = file.version
    request.state.audit_extra = {"path": path, "kind": file.kind, "pii_accessed": file.kind is Kind.PII_JSON}
    return file

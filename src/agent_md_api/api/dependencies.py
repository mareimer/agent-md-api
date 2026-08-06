"""FastAPI-Dependencies — greifen auf Singletons zu, die main.py beim Start in `app.state` ablegt."""

from __future__ import annotations

from fastapi import Depends, Header, Request

from agent_md_api.acl.engine import AclEngine, load_acl_rules
from agent_md_api.audit.sink import AuditSink
from agent_md_api.auth.client_registry import ClientRegistry
from agent_md_api.auth.dependencies import resolve_principal
from agent_md_api.config import Settings
from agent_md_api.domain.errors import AclDeniedError
from agent_md_api.domain.models import Principal
from agent_md_api.storage.git_repo import GitStorage


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_storage(request: Request) -> GitStorage:
    return request.app.state.storage


def get_registry(request: Request) -> ClientRegistry:
    return request.app.state.registry


def get_audit_sink(request: Request) -> AuditSink:
    return request.app.state.audit_sink


def get_acl_engine(request: Request) -> AclEngine:
    """Wird pro Request neu aus /_system/acl.json geladen (spec.md §8) — bei den erwarteten
    Aufrufraten (bis zu 10 Agenten, burstig) kein spürbarer Overhead; kein Caching-Invalidierungs-
    problem, das man sich vorab einhandeln müsste."""
    storage: GitStorage = request.app.state.storage
    return AclEngine(load_acl_rules(storage))


def get_principal(
    request: Request,
    registry: ClientRegistry = Depends(get_registry),
    authorization: str | None = Header(default=None),
    x_user_token: str | None = Header(default=None, alias="X-User-Token"),
) -> Principal:
    """spec.md §10 — wirft UnauthorizedError bei fehlender/ungültiger Auth. Stellt das
    Ergebnis zusätzlich in `request.state.principal`, damit die Audit-Middleware (die
    außerhalb des FastAPI-Dependency-Systems läuft) darauf zugreifen kann."""
    principal = resolve_principal(
        authorization_header=authorization, user_token_header=x_user_token, registry=registry
    )
    request.state.principal = principal
    return principal


def require_admin(request: Request, principal: Principal = Depends(get_principal)) -> Principal:
    settings: Settings = request.app.state.settings
    if principal.user_id != settings.admin_user_id:
        raise AclDeniedError(f"Admin-Endpunkt — nur {settings.admin_user_id} hat Zugriff.")
    return principal

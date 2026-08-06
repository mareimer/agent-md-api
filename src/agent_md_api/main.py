"""FastAPI-App-Wiring (spec.md, docs/openapi.yaml). Uvicorn-Ziel: `agent_md_api.main:app`."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from filelock import FileLock, Timeout

from agent_md_api.api import audit_query, clients, files, tree, transactions
from agent_md_api.api.error_handlers import register_error_handlers
from agent_md_api.audit.factory import build_audit_sink
from agent_md_api.auth.client_registry import ClientRegistry
from agent_md_api.config import Settings, get_settings
from agent_md_api.middleware.audit_middleware import AuditMiddleware
from agent_md_api.storage.git_repo import GitStorage


def _acquire_single_instance_lock(settings: Settings) -> FileLock:
    """spec.md §6: Abbruch statt stillem Bruch der Ein-Instanz-Garantie. Ein zweiter Worker
    (egal ob --workers>1 oder eine zweite Container-Replica auf demselben Mount) bekommt den
    Lock nicht und muss beim Start hart fehlschlagen — ein reiner In-Process-Check würde das
    NICHT erkennen, da mehrere Uvicorn-Worker separate Prozesse ohne gemeinsamen Speicher sind."""
    settings.instance_lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(settings.instance_lock_path))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        print(
            f"FATAL: {settings.instance_lock_path} ist bereits gesperrt — es läuft schon eine "
            "Agent-API-Instanz gegen diesen Arbeitsbaum. Genau ein Worker-Prozess ist eine harte "
            "Voraussetzung (spec.md §6), keine Konfigurationsoption. Prüfe --workers (muss 1 sein) "
            "und ob versehentlich eine zweite Container-Replica läuft.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    return lock


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    lock = _acquire_single_instance_lock(settings)

    app.state.settings = settings
    app.state.storage = GitStorage(settings.root_dir)
    app.state.registry = ClientRegistry(settings.client_registry_path)
    app.state.audit_sink = build_audit_sink(settings)

    yield

    lock.release()


def create_app() -> FastAPI:
    app = FastAPI(title="Agent-API", version="0.1.0", lifespan=lifespan)
    register_error_handlers(app)
    app.add_middleware(AuditMiddleware)

    api_v1 = APIRouter(prefix="/api/v1")
    for router_module in (tree, files, transactions, clients, audit_query):
        api_v1.include_router(router_module.router)
    app.include_router(api_v1)

    return app


app = create_app()

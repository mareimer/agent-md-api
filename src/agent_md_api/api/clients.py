"""/system/clients — Client-Registry-Verwaltung, Admin-only (spec.md §10a)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from agent_md_api.api.dependencies import get_registry, require_admin
from agent_md_api.api.schemas import ClientCreateRequest, ClientUpdateRequest, SigningKeyCreateRequest
from agent_md_api.auth.client_registry import ClientRegistry
from agent_md_api.domain.models import ClientRegistryEntryPublic, ClientType, SigningKey

router = APIRouter(tags=["Clients"], dependencies=[Depends(require_admin)])


@router.get("/system/clients")
def list_clients(registry: ClientRegistry = Depends(get_registry)) -> list[ClientRegistryEntryPublic]:
    return [ClientRegistryEntryPublic.from_entry(e) for e in registry.list_all()]


@router.post("/system/clients", status_code=201)
def create_client(body: ClientCreateRequest, registry: ClientRegistry = Depends(get_registry)) -> dict:
    entry, api_key = registry.create_client(
        client_id=body.client_id,
        type=ClientType(body.type),
        issues_user_tokens=body.issues_user_tokens,
        fixed_user_id=body.fixed_user_id,
    )
    public = ClientRegistryEntryPublic.from_entry(entry)
    return {**public.model_dump(), "api_key": api_key}


@router.get("/system/clients/{client_id}")
def get_client(client_id: str, registry: ClientRegistry = Depends(get_registry)) -> ClientRegistryEntryPublic:
    return ClientRegistryEntryPublic.from_entry(registry.get(client_id))


@router.patch("/system/clients/{client_id}")
def update_client(
    client_id: str, body: ClientUpdateRequest, registry: ClientRegistry = Depends(get_registry)
) -> ClientRegistryEntryPublic:
    entry = registry.update_client(client_id, issues_user_tokens=body.issues_user_tokens)
    return ClientRegistryEntryPublic.from_entry(entry)


@router.delete("/system/clients/{client_id}", status_code=204)
def revoke_client(client_id: str, registry: ClientRegistry = Depends(get_registry)) -> None:
    registry.revoke_client(client_id)


@router.post("/system/clients/{client_id}/rotate-api-key")
def rotate_api_key(client_id: str, registry: ClientRegistry = Depends(get_registry)) -> dict:
    api_key = registry.rotate_api_key(client_id)
    return {"client_id": client_id, "api_key": api_key}


@router.post("/system/clients/{client_id}/signing-keys", status_code=201)
def add_signing_key(
    client_id: str, body: SigningKeyCreateRequest, registry: ClientRegistry = Depends(get_registry)
) -> SigningKey:
    return registry.add_signing_key(client_id, kid=body.kid, public_key=body.public_key, algorithm=body.algorithm)


@router.delete("/system/clients/{client_id}/signing-keys/{kid}", status_code=204)
def revoke_signing_key(client_id: str, kid: str, registry: ClientRegistry = Depends(get_registry)) -> None:
    registry.revoke_signing_key(client_id, kid)

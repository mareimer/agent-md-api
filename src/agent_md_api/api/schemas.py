"""Request-Bodies — spiegeln docs/openapi.yaml. `user_id`/`client_id` bewusst NICHT hier
(kommen aus Auth-Headern, spec.md §5/§10 — gefundene Inkonsistenz in einer früheren Fassung)."""

from __future__ import annotations

from pydantic import BaseModel

from agent_md_api.domain.models import TransactionOperation


class EditRequest(BaseModel):
    old_str: str
    new_str: str
    if_version: str
    reason: str
    task_id: str | None = None


class AppendRequest(BaseModel):
    content: str
    if_version: str
    reason: str
    task_id: str | None = None


class FullWriteTextRequest(BaseModel):
    content: str
    if_version: str | None = None
    reason: str
    task_id: str | None = None


class DeleteRequest(BaseModel):
    if_version: str
    reason: str
    task_id: str | None = None


class TransactionRequest(BaseModel):
    reason: str
    task_id: str | None = None
    operations: list[TransactionOperation]


class ClientCreateRequest(BaseModel):
    client_id: str
    type: str
    issues_user_tokens: bool = False
    fixed_user_id: str | None = None


class ClientUpdateRequest(BaseModel):
    issues_user_tokens: bool | None = None


class SigningKeyCreateRequest(BaseModel):
    kid: str
    public_key: str
    algorithm: str = "EdDSA"

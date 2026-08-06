"""Domänenmodelle — spiegeln docs/openapi.yaml Komponenten 1:1."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class Kind(StrEnum):
    DIR = "dir"
    MD = "md"
    JSON = "json"
    PII_JSON = "pii.json"
    BINARY = "binary"


TEXT_KINDS = {Kind.MD, Kind.JSON, Kind.PII_JSON}


class TreeEntry(BaseModel):
    path: str
    kind: Kind
    size: int | None = None
    version: str | None = None
    preview: str | None = None
    mime_type: str | None = None


class TreeResponse(BaseModel):
    entries: list[TreeEntry]
    next_cursor: str | None = None


class FileResponse(BaseModel):
    path: str
    kind: Kind
    version: str
    size: int | None = None
    mime_type: str | None = None
    content: str | None = None


class WriteResult(BaseModel):
    path: str
    version: str
    commit_id: str


class TransactionOperationType(StrEnum):
    EDIT = "edit"
    APPEND = "append"
    WRITE = "write"
    DELETE = "delete"


class TransactionOperation(BaseModel):
    path: str
    type: TransactionOperationType
    old_str: str | None = None
    new_str: str | None = None
    content: str | None = None
    if_version: str | None = None


class TransactionResultFile(BaseModel):
    path: str
    version: str


class TransactionResult(BaseModel):
    commit_id: str
    files: list[TransactionResultFile]


class AuditOperation(StrEnum):
    TREE = "tree"
    READ = "read"
    READ_CONTENT = "read_content"
    EDIT = "edit"
    APPEND = "append"
    WRITE = "write"
    DELETE = "delete"
    TRANSACTION = "transaction"


class AuditResult(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"


class AuditLogEntry(BaseModel):
    id: str
    timestamp: str
    user_id: str
    client_id: str
    operation: AuditOperation
    path: str | list[str] | None = None
    kind: Kind | None = None
    pii_accessed: bool = False
    commit_id: str | None = None
    reason: str | None = None
    result: AuditResult
    task_id: str | None = None


class AuditLogResponse(BaseModel):
    entries: list[AuditLogEntry]
    next_cursor: str | None = None


class AuditQueryFilter(BaseModel):
    user_id: str | None = None
    path_prefix: str | None = None
    pii_only: bool = False
    operation: AuditOperation | None = None
    from_ts: str | None = None
    to_ts: str | None = None
    cursor: str | None = None


class Principal(BaseModel):
    """Ergebnis der Auth-Verifikation (spec.md §10) — nie aus dem Request-Body übernommen."""

    user_id: str
    client_id: str


class ClientType(StrEnum):
    BFF = "bff"
    ORCHESTRATOR = "orchestrator"
    AUTONOMOUS_AGENT = "autonomous-agent"


class SigningKey(BaseModel):
    kid: str
    public_key: str
    """PEM-kodierter Ed25519-Public-Key."""
    algorithm: str = "EdDSA"
    active: bool = True
    created_at: str
    revoked_at: str | None = None


class ClientRegistryEntry(BaseModel):
    """Interne Sicht — enthält `api_key_hash`, wird NIE über die API ausgegeben (spec.md §10a)."""

    client_id: str
    type: ClientType
    api_key_hash: str
    issues_user_tokens: bool = False
    signing_keys: list[SigningKey] = []
    fixed_user_id: str | None = None
    revoked: bool = False
    created_at: str


class ClientRegistryEntryPublic(BaseModel):
    """Öffentliche Sicht für GET /system/clients — ohne api_key_hash (openapi.yaml)."""

    client_id: str
    type: ClientType
    issues_user_tokens: bool
    signing_keys: list[SigningKey] = []
    fixed_user_id: str | None = None
    revoked: bool
    created_at: str

    @classmethod
    def from_entry(cls, entry: ClientRegistryEntry) -> "ClientRegistryEntryPublic":
        return cls(
            client_id=entry.client_id,
            type=entry.type,
            issues_user_tokens=entry.issues_user_tokens,
            signing_keys=entry.signing_keys,
            fixed_user_id=entry.fixed_user_id,
            revoked=entry.revoked,
            created_at=entry.created_at,
        )


class Permission(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class AclRule(BaseModel):
    """Regel-Modell für /_system/acl.json (spec.md §8).

    Verallgemeinert "scope: user/client" aus der Design-Vorlage: eine Regel
    kann `user_id`, `client_id`, `path_prefix`, `kind` beliebig kombinieren
    (alle optional = Wildcard). Sind sowohl `user_id` als auch `client_id`
    gesetzt, ist es eine Paar-Regel. Siehe acl/engine.py für die
    Spezifitäts-/Vorrangsreihenfolge.

    `path_prefix` bleibt bei einem Muster ohne `*` ein reiner Literal-Präfix
    (unverändertes Verhalten). Enthält das Muster `*`, wird stattdessen als
    Glob gegen den VOLLEN Pfad gematcht (nicht nur als Präfix) — `*` matcht
    beliebige Zeichen außer `/` (bleibt innerhalb eines Pfadsegments), `**`
    matcht auch über `/` hinweg. Beispiel: `"mietvertrag*.*"` matcht
    `mietvertrag.md`, `mietvertrag.json`, `mietvertrag.pii.json`, aber nicht
    `kontakte/mietvertrag.md` (kein Verzeichniswechsel ohne `**`).
    """

    user_id: str | None = None
    client_id: str | None = None
    path_prefix: str | None = None
    kind: Kind | None = None
    read: Permission | None = None
    write: Permission | None = None

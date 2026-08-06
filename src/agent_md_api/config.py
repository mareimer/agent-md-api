"""Konfiguration ausschließlich über Env-Vars (deployment.md §2)."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuditBackend(StrEnum):
    NONE = "none"
    SQLITE = "sqlite"
    JSONL = "jsonl"
    LOKI = "loki"
    ELK = "elk"
    AZURE = "azure"
    AWS = "aws"


NOT_YET_IMPLEMENTED = {AuditBackend.LOKI, AuditBackend.ELK, AuditBackend.AZURE, AuditBackend.AWS}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_API_", extra="ignore")

    root_dir: Path
    """Git-Arbeitsbaum, siehe spec.md §3. Muss beim Start bereits ein Git-Repo sein."""

    audit_backend: AuditBackend = AuditBackend.SQLITE
    audit_db_path: Path | None = None
    """Nur bei audit_backend=sqlite (spec.md §9.3)."""
    audit_log_path: Path | None = None
    """Nur bei audit_backend=jsonl (spec.md §9.3)."""

    client_registry_path: Path = Path("/data/clients/clients.json")
    """Eigene, kleine Verwaltung außerhalb des Git-Arbeitsbaums (spec.md §10a)."""

    instance_lock_path: Path = Path("/data/instance.lock")
    """spec.md §6 Ein-Instanz-Garantie: Datei-Lock statt In-Process-Check, weil mehrere
    Uvicorn-Worker SEPARATE Prozesse ohne gemeinsamen Speicher sind — ein In-Process-Flag
    würde die Mehrfachausführung nicht erkennen, ein exklusiver Datei-Lock schon."""

    max_retry_on_conflict: int = 3
    """spec.md §11 — Client-seitige Empfehlung, hier nur als Konstante für Doku/Tests gehalten."""

    admin_user_id: str = "human:admin"
    """Einziger user_id mit Schreibrecht auf /_system/acl.json und den Client-/Audit-Admin-Endpunkten (spec.md §8/§10).

    Der Default ist bewusst ein Platzhalter, der auf kein reales Konto zeigt: Wird
    `AGENT_API_ADMIN_USER_ID` nicht gesetzt, hat schlicht niemand Adminrechte
    (fail-closed) — statt sie versehentlich einer beliebigen Identität zu geben."""

    @model_validator(mode="after")
    def _check_audit_backend_config(self) -> "Settings":
        if self.audit_backend in NOT_YET_IMPLEMENTED:
            raise ValueError(
                f"AGENT_API_AUDIT_BACKEND={self.audit_backend} ist noch nicht implementiert "
                "(spec.md §9.3) — nutze 'none', 'sqlite' oder 'jsonl'."
            )
        if self.audit_backend is AuditBackend.SQLITE and self.audit_db_path is None:
            self.audit_db_path = Path("/data/audit/audit.db")
        if self.audit_backend is AuditBackend.JSONL and self.audit_log_path is None:
            self.audit_log_path = Path("/data/audit/audit.jsonl")
        return self


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # Werte kommen aus Env-Vars

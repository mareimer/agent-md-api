"""Konfiguration ausschließlich über Env-Vars, Präfix WEBDAV_BRIDGE_ (spec.md §14.2)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WEBDAV_BRIDGE_", extra="ignore")

    agent_api_base_url: str
    """z.B. http://agent-api:8000 (Docker-Compose-Servicename, deployment.md §3)."""

    agent_api_client_id: str = "webdav-bridge"
    agent_api_client_api_key: str
    """Client-API-Key, den die Bridge bei /system/clients für sich selbst erhalten hat."""

    signing_kid: str
    signing_private_key_path: Path
    """PEM-Datei mit dem privaten Ed25519-Schlüssel der Bridge — verlässt die Bridge nie
    (spec.md §10b/§14.2). Der zugehörige Public Key ist bei der Agent-API registriert."""

    user_access_path: Path = Path("/data/webdav-users.json")
    """Eigene, kleine Zugangsverwaltung: Benutzername -> user_id + PAT-Hash (spec.md §14.2)."""

    host: str = "0.0.0.0"
    port: int = 8000

    user_token_ttl_seconds: int = 900


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

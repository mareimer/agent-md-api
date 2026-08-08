"""Eigene, kleine Zugangsverwaltung für die WebDAV-Bridge (spec.md §14.2).

Unabhängig von der Agent-API-Client-Registry — das ist eine reine
Mensch-Zugangsebene *vor* der Bridge (Windows-WebDAV-Client kann nur Basic-
Auth, kein X-User-Token). Persistenz: JSON-Datei, analog zum Muster in
`agent_md_api.auth.client_registry`. SHA-256-Hashing wie dort begründet
(hochentropische Personal-Access-Tokens, kein Argon2 nötig).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from datetime import UTC, datetime
from pathlib import Path


def _hash_pat(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class UnknownUserError(Exception):
    pass


class UserAccessRegistry:
    """Benutzername -> {agent_api_user_id, pat_hash}.

    Liest die JSON-Datei bei **jedem** Zugriff (read und write) neu von der Platte,
    statt sie nur einmal beim Prozessstart zu laden — aus demselben Grund wie bei
    `agent_md_api.auth.client_registry.ClientRegistry`: die Benutzerverwaltungs-CLI
    (`webdav_bridge.cli`) läuft als eigener, kurzlebiger Prozess neben dem laufenden
    Bridge-Server und schreibt in dieselbe Datei — ohne Reload-on-Access würde ein per
    CLI angelegter/rotierter Zugang vom schon laufenden Server erst nach einem Neustart
    gesehen."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._by_username: dict[str, dict] = {}
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _reload(self) -> None:
        if not self._path.exists():
            self._by_username = {}
            return
        self._by_username = json.loads(self._path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._by_username, indent=2), encoding="utf-8")

    def create_user(self, *, username: str, agent_api_user_id: str) -> str:
        """Legt einen Zugang an, gibt das neu generierte Personal-Access-Token
        im Klartext zurück (wird nur hier einmalig ausgegeben)."""
        with self._lock:
            self._reload()
            raw_pat = secrets.token_urlsafe(24)
            self._by_username[username] = {
                "agent_api_user_id": agent_api_user_id,
                "pat_hash": _hash_pat(raw_pat),
                "created_at": datetime.now(UTC).isoformat(),
            }
            self._save()
            return raw_pat

    def rotate_token(self, username: str) -> str:
        with self._lock:
            self._reload()
            if username not in self._by_username:
                raise UnknownUserError(username)
            raw_pat = secrets.token_urlsafe(24)
            self._by_username[username]["pat_hash"] = _hash_pat(raw_pat)
            self._save()
            return raw_pat

    def revoke_user(self, username: str) -> None:
        with self._lock:
            self._reload()
            self._by_username.pop(username, None)
            self._save()

    def list_usernames(self) -> list[str]:
        with self._lock:
            self._reload()
            return sorted(self._by_username)

    def verify(self, *, username: str, password: str) -> str | None:
        """Gibt die agent_api_user_id zurück, wenn Benutzername+PAT gültig sind, sonst None."""
        with self._lock:
            self._reload()
            entry = self._by_username.get(username)
            if entry is None:
                return None
            if entry["pat_hash"] != _hash_pat(password):
                return None
            return entry["agent_api_user_id"]

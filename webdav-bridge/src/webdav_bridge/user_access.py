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
    """Benutzername -> {agent_api_user_id, pat_hash}."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._by_username: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            return
        self._by_username = json.loads(self._path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._by_username, indent=2), encoding="utf-8")

    def create_user(self, *, username: str, agent_api_user_id: str) -> str:
        """Legt einen Zugang an, gibt das neu generierte Personal-Access-Token
        im Klartext zurück (wird nur hier einmalig ausgegeben)."""
        with self._lock:
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
            if username not in self._by_username:
                raise UnknownUserError(username)
            raw_pat = secrets.token_urlsafe(24)
            self._by_username[username]["pat_hash"] = _hash_pat(raw_pat)
            self._save()
            return raw_pat

    def revoke_user(self, username: str) -> None:
        with self._lock:
            self._by_username.pop(username, None)
            self._save()

    def verify(self, *, username: str, password: str) -> str | None:
        """Gibt die agent_api_user_id zurück, wenn Benutzername+PAT gültig sind, sonst None."""
        entry = self._by_username.get(username)
        if entry is None:
            return None
        if entry["pat_hash"] != _hash_pat(password):
            return None
        return entry["agent_api_user_id"]

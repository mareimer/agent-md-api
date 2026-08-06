"""Client-Registry (spec.md §10a) — eigene, kleine Verwaltung außerhalb des Git-Arbeitsbaums.

Persistenz: JSON-Datei mit eigenem Lock (unabhängig vom Git-Storage-Lock —
andere Ressource). Bei diesem erwarteten Umfang (bis zu 10 Clients) kein
Overhead durch eine eigene DB nötig.

API-Key-Hashing: SHA-256 statt Argon2. Argon2 ist für **niedrig-entropische**
Geheimnisse (Passwörter) gedacht, die langsames Hashing gegen Brute-Force
brauchen. Unsere API-Keys sind hochentropische Zufallstoken
(`secrets.token_urlsafe(32)`) — dagegen reicht ein schneller kryptografischer
Hash, ohne die zusätzliche Abhängigkeit `argon2-cffi`. Falls gewünscht,
später austauschbar (eigene Funktion `_hash_api_key`).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from datetime import UTC, datetime
from pathlib import Path

from agent_md_api.domain.errors import NotFoundError, UnauthorizedError, ValidationError
from agent_md_api.domain.models import ClientRegistryEntry, ClientType, SigningKey


def _hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ClientRegistry:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._by_client_id: dict[str, ClientRegistryEntry] = {}
        self._by_api_key_hash: dict[str, str] = {}  # hash -> client_id
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            return
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        for item in raw:
            entry = ClientRegistryEntry.model_validate(item)
            self._by_client_id[entry.client_id] = entry
            self._by_api_key_hash[entry.api_key_hash] = entry.client_id

    def _save(self) -> None:
        data = [e.model_dump() for e in self._by_client_id.values()]
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # ---- Verifikation (Hot Path, spec.md §10) ------------------------------------

    def verify_api_key(self, raw_key: str) -> ClientRegistryEntry:
        client_id = self._by_api_key_hash.get(_hash_api_key(raw_key))
        if client_id is None:
            raise UnauthorizedError("Ungültiger Client-API-Key.")
        entry = self._by_client_id[client_id]
        if entry.revoked:
            raise UnauthorizedError(f"Client '{client_id}' ist widerrufen.")
        return entry

    def get(self, client_id: str) -> ClientRegistryEntry:
        entry = self._by_client_id.get(client_id)
        if entry is None:
            raise NotFoundError(f"Client '{client_id}' nicht gefunden.")
        return entry

    def list_all(self) -> list[ClientRegistryEntry]:
        return list(self._by_client_id.values())

    def active_signing_key(self, client_id: str, kid: str) -> SigningKey | None:
        entry = self.get(client_id)
        for key in entry.signing_keys:
            if key.kid == kid and key.active and key.revoked_at is None:
                return key
        return None

    # ---- Verwaltung (Admin-only, spec.md §10a) -----------------------------------

    def create_client(
        self,
        *,
        client_id: str,
        type: ClientType,
        issues_user_tokens: bool = False,
        fixed_user_id: str | None = None,
    ) -> tuple[ClientRegistryEntry, str]:
        with self._lock:
            if client_id in self._by_client_id:
                raise ValidationError(f"Client '{client_id}' existiert bereits.")
            if type is ClientType.AUTONOMOUS_AGENT and not fixed_user_id:
                raise ValidationError("fixed_user_id ist Pflicht bei type=autonomous-agent.")
            if type is not ClientType.AUTONOMOUS_AGENT and fixed_user_id:
                raise ValidationError("fixed_user_id ist nur bei type=autonomous-agent zulässig.")

            raw_key = secrets.token_urlsafe(32)
            entry = ClientRegistryEntry(
                client_id=client_id,
                type=type,
                api_key_hash=_hash_api_key(raw_key),
                issues_user_tokens=issues_user_tokens,
                fixed_user_id=fixed_user_id,
                created_at=_now(),
            )
            self._by_client_id[client_id] = entry
            self._by_api_key_hash[entry.api_key_hash] = client_id
            self._save()
            return entry, raw_key

    def update_client(self, client_id: str, *, issues_user_tokens: bool | None = None) -> ClientRegistryEntry:
        with self._lock:
            entry = self.get(client_id)
            if issues_user_tokens is not None:
                entry.issues_user_tokens = issues_user_tokens
            self._save()
            return entry

    def revoke_client(self, client_id: str) -> None:
        with self._lock:
            entry = self.get(client_id)
            entry.revoked = True
            self._save()

    def rotate_api_key(self, client_id: str) -> str:
        with self._lock:
            entry = self.get(client_id)
            del self._by_api_key_hash[entry.api_key_hash]
            raw_key = secrets.token_urlsafe(32)
            entry.api_key_hash = _hash_api_key(raw_key)
            self._by_api_key_hash[entry.api_key_hash] = client_id
            self._save()
            return raw_key

    def add_signing_key(self, client_id: str, *, kid: str, public_key: str, algorithm: str = "EdDSA") -> SigningKey:
        with self._lock:
            entry = self.get(client_id)
            if any(k.kid == kid for k in entry.signing_keys):
                raise ValidationError(f"kid '{kid}' existiert bereits bei Client '{client_id}'.")
            key = SigningKey(kid=kid, public_key=public_key, algorithm=algorithm, created_at=_now())
            entry.signing_keys.append(key)
            self._save()
            return key

    def revoke_signing_key(self, client_id: str, kid: str) -> None:
        with self._lock:
            entry = self.get(client_id)
            for key in entry.signing_keys:
                if key.kid == kid:
                    key.active = False
                    key.revoked_at = _now()
                    self._save()
                    return
            raise NotFoundError(f"kid '{kid}' bei Client '{client_id}' nicht gefunden.")

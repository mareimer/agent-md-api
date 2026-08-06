"""HTTP-Client gegen die Agent-API (spec.md §14.2/§14.3).

Mintet pro `user_id` ein kurzlebiges User-Token (EdDSA, wie spec.md §10b) und
cached es bis kurz vor Ablauf — vermeidet, bei jedem einzelnen WebDAV-
Request (PROPFIND-Stürme sind normal) neu zu signieren.

Bewusst KEINE Abhängigkeit auf `agent_md_api` (den Kern) — die Bridge ist ein
eigenes Deployable (deployment.md §3), ein kleines Stück JWT-Minting hier
dupliziert zu haben ist die richtige Grenze, nicht eine Python-Importkopplung
zwischen zwei separat deploybaren Services."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key


class AgentApiError(Exception):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class AgentApiNotFoundError(AgentApiError):
    pass


class AgentApiForbiddenError(AgentApiError):
    pass


class AgentApiConflictError(AgentApiError):
    def __init__(self, message: str, *, current_content: str | None, current_version: str) -> None:
        super().__init__(message, status_code=409)
        self.current_content = current_content
        self.current_version = current_version


@dataclass
class FileMeta:
    path: str
    kind: str
    version: str
    size: int | None
    mime_type: str | None
    content: str | None  # nur Text-Kinds


@dataclass
class TreeEntryMeta:
    path: str
    kind: str
    version: str | None
    size: int | None
    mime_type: str | None


def _raise_for_error(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        body = response.json()
    except Exception:
        body = {}
    message = body.get("message", response.text)
    if response.status_code == 409:
        raise AgentApiConflictError(
            message, current_content=body.get("current_content"), current_version=body.get("current_version", "")
        )
    if response.status_code == 404:
        raise AgentApiNotFoundError(message, status_code=404)
    if response.status_code in (401, 403):
        raise AgentApiForbiddenError(message, status_code=response.status_code)
    raise AgentApiError(message, status_code=response.status_code)


class _TokenCache:
    def __init__(self, *, client_id: str, kid: str, private_key: Ed25519PrivateKey, ttl_seconds: int) -> None:
        self._client_id = client_id
        self._kid = kid
        self._private_key = private_key
        self._ttl = ttl_seconds
        self._tokens: dict[str, tuple[str, float]] = {}  # user_id -> (token, expires_at)

    def get(self, user_id: str) -> str:
        cached = self._tokens.get(user_id)
        now = time.time()
        if cached and cached[1] - now > 60:  # mind. 60s Puffer vor Ablauf
            return cached[0]
        now_i = int(now)
        payload = {
            "iss": self._client_id, "sub": user_id, "iat": now_i,
            "exp": now_i + self._ttl, "jti": uuid.uuid4().hex,
        }
        token = jwt.encode(payload, self._private_key, algorithm="EdDSA", headers={"kid": self._kid})
        self._tokens[user_id] = (token, now + self._ttl)
        return token


class AgentApiClient:
    def __init__(
        self, *, base_url: str, client_id: str, client_api_key: str,
        signing_kid: str, signing_private_key_path: Path, user_token_ttl_seconds: int = 900,
    ) -> None:
        self._http = httpx.Client(base_url=base_url.rstrip("/") + "/api/v1", timeout=30.0)
        self._client_api_key = client_api_key
        private_key = load_pem_private_key(signing_private_key_path.read_bytes(), password=None)
        self._tokens = _TokenCache(
            client_id=client_id, kid=signing_kid, private_key=private_key, ttl_seconds=user_token_ttl_seconds
        )

    def _headers(self, user_id: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._client_api_key}",
            "X-User-Token": self._tokens.get(user_id),
        }

    def list_tree(self, *, user_id: str, root: str, depth: int = 1) -> list[TreeEntryMeta]:
        r = self._http.get("/tree", params={"root": root, "depth": depth}, headers=self._headers(user_id))
        _raise_for_error(r)
        data = r.json()
        return [
            TreeEntryMeta(path=e["path"], kind=e["kind"], version=e.get("version"), size=e.get("size"), mime_type=e.get("mime_type"))
            for e in data["entries"]
        ]

    def get_file(self, *, user_id: str, path: str) -> FileMeta:
        r = self._http.get(f"/file/{path}", headers=self._headers(user_id))
        _raise_for_error(r)
        data = r.json()
        return FileMeta(
            path=data["path"], kind=data["kind"], version=data["version"],
            size=data.get("size"), mime_type=data.get("mime_type"), content=data.get("content"),
        )

    def get_file_content(self, *, user_id: str, path: str) -> bytes:
        r = self._http.get(f"/file/{path}/content", headers=self._headers(user_id))
        _raise_for_error(r)
        return r.content

    def write_text(self, *, user_id: str, path: str, content: str, if_version: str | None, reason: str, task_id: str | None = None) -> str:
        """Gibt die neue Version zurück."""
        body: dict = {"content": content, "reason": reason}
        if if_version is not None:
            body["if_version"] = if_version
        if task_id:
            body["task_id"] = task_id
        r = self._http.post(f"/file/{path}", json=body, headers=self._headers(user_id))
        _raise_for_error(r)
        return r.json()["version"]

    def write_binary(self, *, user_id: str, path: str, data: bytes, if_version: str | None, reason: str, task_id: str | None = None) -> str:
        form = {"reason": reason}
        if if_version is not None:
            form["if_version"] = if_version
        if task_id:
            form["task_id"] = task_id
        files = {"file": (path.rsplit("/", 1)[-1], data, "application/octet-stream")}
        r = self._http.post(f"/file/{path}", data=form, files=files, headers=self._headers(user_id))
        _raise_for_error(r)
        return r.json()["version"]

    def delete(self, *, user_id: str, path: str, if_version: str, reason: str, task_id: str | None = None) -> None:
        body = {"if_version": if_version, "reason": reason}
        if task_id:
            body["task_id"] = task_id
        r = self._http.request("DELETE", f"/file/{path}", json=body, headers=self._headers(user_id))
        _raise_for_error(r)

    def move_text(self, *, user_id: str, src_path: str, dest_path: str, content: str, src_version: str, reason: str) -> None:
        """Atomare Transaktion: write(dest) + delete(src) in einem Commit (spec.md §14.3)."""
        body = {
            "reason": reason,
            "operations": [
                {"path": dest_path, "type": "write", "content": content},
                {"path": src_path, "type": "delete", "if_version": src_version},
            ],
        }
        r = self._http.post("/transaction", json=body, headers=self._headers(user_id))
        _raise_for_error(r)

    def copy_text(self, *, user_id: str, dest_path: str, content: str, reason: str) -> None:
        r = self._http.post(
            "/transaction",
            json={"reason": reason, "operations": [{"path": dest_path, "type": "write", "content": content}]},
            headers=self._headers(user_id),
        )
        _raise_for_error(r)

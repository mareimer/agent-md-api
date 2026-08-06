"""User-Token: signierter Kurzzeit-JWT, EdDSA/Ed25519 (spec.md §10b).

Der private Schlüssel gehört dem ausstellenden Client (z.B. Web-BFF) und
verlässt diesen nie — die Agent-API sieht nur Public Keys aus der
Client-Registry. Diese Datei kennt daher nur Verifikation + eine
Hilfsfunktion zum Minten (nützlich für Tests und für Clients, die in
Python geschrieben sind, z.B. die WebDAV-Bridge in diesem Repo).
"""

from __future__ import annotations

import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
    load_pem_public_key,
)

from agent_md_api.domain.errors import UnauthorizedError

DEFAULT_TTL_SECONDS = 15 * 60


def generate_keypair_pem() -> tuple[str, str]:
    """Für Tests/Bootstrapping — Client generiert sein eigenes Keypair normalerweise selbst."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode("ascii")
    public_pem = public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    return private_pem, public_pem


def mint_user_token(*, user_id: str, client_id: str, kid: str, private_key_pem: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    private_key: Ed25519PrivateKey = load_pem_private_key(private_key_pem.encode("ascii"), password=None)  # type: ignore[assignment]
    now = int(time.time())
    payload = {"iss": client_id, "sub": user_id, "iat": now, "exp": now + ttl_seconds, "jti": uuid.uuid4().hex}
    return jwt.encode(payload, private_key, algorithm="EdDSA", headers={"kid": kid})


def peek_kid(token: str) -> str:
    """Liest den `kid` aus dem (noch unverifizierten) Header — nötig, um den richtigen Public Key zu finden."""
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if not kid:
        raise UnauthorizedError("X-User-Token ohne `kid` im Header.")
    return kid


def verify_user_token(token: str, *, public_key_pem: str, expected_client_id: str) -> tuple[str, str]:
    """Gibt (user_id, client_id) zurück. `expected_client_id` kommt aus dem bereits verifizierten
    Client-API-Key (spec.md §10b) — der `iss`-Claim im Token wird dagegen geprüft, nie blind übernommen."""
    public_key: Ed25519PublicKey = load_pem_public_key(public_key_pem.encode("ascii"))  # type: ignore[assignment]
    try:
        payload = jwt.decode(token, public_key, algorithms=["EdDSA"], options={"require": ["exp", "iss", "sub"]})
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("X-User-Token abgelaufen.") from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError(f"X-User-Token ungültig: {exc}") from exc

    if payload["iss"] != expected_client_id:
        raise UnauthorizedError("X-User-Token: `iss` stimmt nicht mit dem verifizierten Client überein.")
    return payload["sub"], payload["iss"]

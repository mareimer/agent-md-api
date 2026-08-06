"""Verifikationsreihenfolge aus spec.md §10 — als reine Funktion, unabhängig von FastAPI,
damit sie in main.py als Dependency verdrahtet und isoliert getestet werden kann."""

from __future__ import annotations

from agent_md_api.auth.client_registry import ClientRegistry
from agent_md_api.auth.tokens import peek_kid, verify_user_token
from agent_md_api.domain.errors import UnauthorizedError
from agent_md_api.domain.models import Principal


def resolve_principal(
    *,
    authorization_header: str | None,
    user_token_header: str | None,
    registry: ClientRegistry,
) -> Principal:
    """spec.md §10b Verifikationsreihenfolge:
    1. Client-API-Key aus Authorization-Header -> verifizierter client_id.
    2. Falls X-User-Token vorhanden: kid -> Public Key des VERIFIZIERTEN Clients (nicht aus
       dem Token selbst) -> Signatur/exp/iss prüfen -> user_id = sub.
    3. Sonst: Client muss fixed_user_id gesetzt haben (autonomer Agent).
    """
    if not authorization_header or not authorization_header.startswith("Bearer "):
        raise UnauthorizedError("Authorization: Bearer <client_api_key> fehlt.")
    raw_key = authorization_header.removeprefix("Bearer ").strip()
    client = registry.verify_api_key(raw_key)

    if user_token_header:
        kid = peek_kid(user_token_header)
        signing_key = registry.active_signing_key(client.client_id, kid)
        if signing_key is None:
            raise UnauthorizedError(f"Kein aktiver Signing-Key '{kid}' für Client '{client.client_id}'.")
        user_id, verified_client_id = verify_user_token(
            user_token_header, public_key_pem=signing_key.public_key, expected_client_id=client.client_id
        )
        return Principal(user_id=user_id, client_id=verified_client_id)

    if client.fixed_user_id:
        return Principal(user_id=client.fixed_user_id, client_id=client.client_id)

    raise UnauthorizedError(
        f"Client '{client.client_id}' hat kein fixed_user_id — X-User-Token ist erforderlich."
    )

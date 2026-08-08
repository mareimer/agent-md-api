"""Client-Registry + User-Token-Verifikation (spec.md §10), ohne HTTP-Layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_md_api.auth.client_registry import ClientRegistry
from agent_md_api.auth.dependencies import resolve_principal
from agent_md_api.auth.tokens import generate_keypair_pem, mint_user_token, verify_user_token
from agent_md_api.domain.errors import NotFoundError, UnauthorizedError, ValidationError
from agent_md_api.domain.models import ClientType


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    return tmp_path / "clients.json"


@pytest.fixture
def registry(registry_path: Path) -> ClientRegistry:
    return ClientRegistry(registry_path)


# ---- Client-API-Key (spec.md §10a) -------------------------------------------------------


def test_create_client_and_verify_api_key_roundtrip(registry: ClientRegistry) -> None:
    entry, raw_key = registry.create_client(client_id="web-bff", type=ClientType.BFF, issues_user_tokens=True)

    verified = registry.verify_api_key(raw_key)

    assert verified.client_id == entry.client_id
    # api_key_hash ist ein Hash, niemals der Klartext-Key selbst.
    assert verified.api_key_hash != raw_key


def test_verify_api_key_wrong_key_raises_unauthorized(registry: ClientRegistry) -> None:
    registry.create_client(client_id="web-bff", type=ClientType.BFF, issues_user_tokens=True)

    with pytest.raises(UnauthorizedError):
        registry.verify_api_key("dieser-key-existiert-nicht")


def test_revoked_client_denied_even_with_correct_key(registry: ClientRegistry) -> None:
    _entry, raw_key = registry.create_client(client_id="web-bff", type=ClientType.BFF, issues_user_tokens=True)
    registry.revoke_client("web-bff")

    with pytest.raises(UnauthorizedError):
        registry.verify_api_key(raw_key)


def test_autonomous_agent_requires_fixed_user_id(registry: ClientRegistry) -> None:
    with pytest.raises(ValidationError):
        registry.create_client(client_id="planner-03", type=ClientType.AUTONOMOUS_AGENT, fixed_user_id=None)


def test_non_autonomous_client_rejects_fixed_user_id(registry: ClientRegistry) -> None:
    with pytest.raises(ValidationError):
        registry.create_client(client_id="web-bff", type=ClientType.BFF, fixed_user_id="system:planner-03")


def test_rotate_api_key_invalidates_old_key(registry: ClientRegistry) -> None:
    registry.create_client(client_id="web-bff", type=ClientType.BFF, issues_user_tokens=True)
    old_key = registry.get("web-bff").api_key_hash
    new_raw_key = registry.rotate_api_key("web-bff")

    assert registry.get("web-bff").api_key_hash != old_key
    verified = registry.verify_api_key(new_raw_key)
    assert verified.client_id == "web-bff"


# ---- Registry-Persistenz über Neuladen ---------------------------------------------------


def test_registry_persists_across_reload(registry_path: Path) -> None:
    reg1 = ClientRegistry(registry_path)
    _entry, raw_key = reg1.create_client(client_id="web-bff", type=ClientType.BFF, issues_user_tokens=True)

    reg2 = ClientRegistry(registry_path)  # frisches Objekt, liest dieselbe Datei neu ein

    verified = reg2.verify_api_key(raw_key)
    assert verified.client_id == "web-bff"


def test_already_running_registry_sees_client_created_by_separate_instance(registry_path: Path) -> None:
    """Regression: die Bootstrap-CLI läuft als eigener Prozess neben dem schon laufenden
    Uvicorn-Worker und schreibt in dieselbe Datei. Ohne Reload-on-Access (spec.md §10a,
    Docstring von `ClientRegistry`) würde ein per CLI angelegter Client erst nach einem
    Neustart des Servers auffindbar — genau das darf hier nicht mehr passieren."""
    server_registry = ClientRegistry(registry_path)  # simuliert die schon laufende App
    cli_registry = ClientRegistry(registry_path)  # simuliert einen separaten CLI-Aufruf

    _entry, raw_key = cli_registry.create_client(client_id="webdav-bridge", type=ClientType.BFF)

    verified = server_registry.verify_api_key(raw_key)
    assert verified.client_id == "webdav-bridge"


def test_already_running_registry_sees_signing_key_added_by_separate_instance(registry_path: Path) -> None:
    server_registry = ClientRegistry(registry_path)
    cli_registry = ClientRegistry(registry_path)
    cli_registry.create_client(client_id="webdav-bridge", type=ClientType.BFF, issues_user_tokens=True)

    _priv, pub = generate_keypair_pem()
    cli_registry.add_signing_key("webdav-bridge", kid="bridge-key-1", public_key=pub)

    key = server_registry.active_signing_key("webdav-bridge", "bridge-key-1")
    assert key is not None
    assert key.public_key == pub


def test_already_running_registry_sees_revocation_by_separate_instance(registry_path: Path) -> None:
    server_registry = ClientRegistry(registry_path)
    cli_registry = ClientRegistry(registry_path)
    _entry, raw_key = cli_registry.create_client(client_id="webdav-bridge", type=ClientType.BFF)

    cli_registry.revoke_client("webdav-bridge")

    with pytest.raises(UnauthorizedError):
        server_registry.verify_api_key(raw_key)


# ---- User-Token minten/verifizieren (spec.md §10b) ---------------------------------------


def test_mint_and_verify_user_token_roundtrip() -> None:
    private_pem, public_pem = generate_keypair_pem()
    token = mint_user_token(user_id="human:alice", client_id="web-bff", kid="k1", private_key_pem=private_pem)

    user_id, client_id = verify_user_token(token, public_key_pem=public_pem, expected_client_id="web-bff")

    assert user_id == "human:alice"
    assert client_id == "web-bff"


def test_verify_user_token_wrong_expected_client_id_rejected() -> None:
    private_pem, public_pem = generate_keypair_pem()
    token = mint_user_token(user_id="human:alice", client_id="web-bff", kid="k1", private_key_pem=private_pem)

    with pytest.raises(UnauthorizedError):
        # `iss` im Token ist "web-bff", aber wir erwarten einen anderen (bereits über den
        # API-Key verifizierten) Client — Schutz gegen Client-Identitäts-Spoofing (spec.md §10b).
        verify_user_token(token, public_key_pem=public_pem, expected_client_id="mobile-bff")


def test_verify_user_token_expired_rejected() -> None:
    private_pem, public_pem = generate_keypair_pem()
    token = mint_user_token(user_id="human:alice", client_id="web-bff", kid="k1", private_key_pem=private_pem, ttl_seconds=-10)

    with pytest.raises(UnauthorizedError):
        verify_user_token(token, public_key_pem=public_pem, expected_client_id="web-bff")


def test_verify_user_token_wrong_public_key_rejected() -> None:
    private_pem, _public_pem = generate_keypair_pem()
    _other_private_pem, wrong_public_pem = generate_keypair_pem()
    token = mint_user_token(user_id="human:alice", client_id="web-bff", kid="k1", private_key_pem=private_pem)

    with pytest.raises(UnauthorizedError):
        verify_user_token(token, public_key_pem=wrong_public_pem, expected_client_id="web-bff")


# ---- Verifikationsreihenfolge resolve_principal() (spec.md §10) -------------------------


def test_resolve_principal_fixed_user_id_path_without_token(registry: ClientRegistry) -> None:
    _entry, raw_key = registry.create_client(
        client_id="planner-03", type=ClientType.AUTONOMOUS_AGENT, fixed_user_id="system:planner-03"
    )

    principal = resolve_principal(authorization_header=f"Bearer {raw_key}", user_token_header=None, registry=registry)

    assert principal.user_id == "system:planner-03"
    assert principal.client_id == "planner-03"


def test_resolve_principal_without_fixed_user_id_and_without_token_raises(registry: ClientRegistry) -> None:
    _entry, raw_key = registry.create_client(client_id="web-bff", type=ClientType.BFF, issues_user_tokens=True)

    with pytest.raises(UnauthorizedError):
        resolve_principal(authorization_header=f"Bearer {raw_key}", user_token_header=None, registry=registry)


def test_resolve_principal_missing_authorization_header_raises(registry: ClientRegistry) -> None:
    with pytest.raises(UnauthorizedError):
        resolve_principal(authorization_header=None, user_token_header=None, registry=registry)


def test_resolve_principal_with_valid_user_token(registry: ClientRegistry) -> None:
    _entry, raw_key = registry.create_client(client_id="web-bff", type=ClientType.BFF, issues_user_tokens=True)
    private_pem, public_pem = generate_keypair_pem()
    registry.add_signing_key("web-bff", kid="k1", public_key=public_pem)
    token = mint_user_token(user_id="human:alice", client_id="web-bff", kid="k1", private_key_pem=private_pem)

    principal = resolve_principal(
        authorization_header=f"Bearer {raw_key}", user_token_header=token, registry=registry
    )

    assert principal.user_id == "human:alice"
    assert principal.client_id == "web-bff"


def test_resolve_principal_revoked_signing_key_rejected(registry: ClientRegistry) -> None:
    _entry, raw_key = registry.create_client(client_id="web-bff", type=ClientType.BFF, issues_user_tokens=True)
    private_pem, public_pem = generate_keypair_pem()
    registry.add_signing_key("web-bff", kid="k1", public_key=public_pem)
    token = mint_user_token(user_id="human:alice", client_id="web-bff", kid="k1", private_key_pem=private_pem)

    registry.revoke_signing_key("web-bff", "k1")

    with pytest.raises(UnauthorizedError):
        resolve_principal(authorization_header=f"Bearer {raw_key}", user_token_header=token, registry=registry)


def test_resolve_principal_unknown_kid_rejected(registry: ClientRegistry) -> None:
    _entry, raw_key = registry.create_client(client_id="web-bff", type=ClientType.BFF, issues_user_tokens=True)
    private_pem, _public_pem = generate_keypair_pem()
    # kein add_signing_key aufgerufen -> "k1" ist dem Client unbekannt.
    token = mint_user_token(user_id="human:alice", client_id="web-bff", kid="k1", private_key_pem=private_pem)

    with pytest.raises(UnauthorizedError):
        resolve_principal(authorization_header=f"Bearer {raw_key}", user_token_header=token, registry=registry)


def test_revoke_signing_key_of_unknown_kid_raises_not_found(registry: ClientRegistry) -> None:
    registry.create_client(client_id="web-bff", type=ClientType.BFF, issues_user_tokens=True)

    with pytest.raises(NotFoundError):
        registry.revoke_signing_key("web-bff", "unbekannter-kid")

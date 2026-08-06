"""HTTP-Layer: /system/clients (CRUD, Rotation, Signing-Keys) + /system/audit (spec.md §9.4/§10a)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_md_api.auth.tokens import generate_keypair_pem
from conftest import AdminContext, bootstrap_admin, configure_env, init_repo

# ---- /system/clients ------------------------------------------------------------------------


def test_create_client_via_admin(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/system/clients",
        json={"client_id": "mobile-bff", "type": "bff", "issues_user_tokens": True},
        headers=admin_headers,
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["client_id"] == "mobile-bff"
    assert "api_key" in body
    assert "api_key_hash" not in body  # niemals über die API ausgeben (spec.md §10a)


def test_list_clients_includes_bootstrap_client(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.get("/api/v1/system/clients", headers=admin_headers)

    assert resp.status_code == 200
    client_ids = {c["client_id"] for c in resp.json()}
    assert "web-bff" in client_ids


def test_get_single_client(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.get("/api/v1/system/clients/web-bff", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["client_id"] == "web-bff"


def test_update_client_issues_user_tokens_flag(client: TestClient, admin_headers: dict[str, str]) -> None:
    client.post(
        "/api/v1/system/clients", json={"client_id": "orchestrator", "type": "orchestrator"}, headers=admin_headers
    )

    resp = client.patch(
        "/api/v1/system/clients/orchestrator", json={"issues_user_tokens": True}, headers=admin_headers
    )

    assert resp.status_code == 200
    assert resp.json()["issues_user_tokens"] is True


def test_revoke_client(client: TestClient, admin_headers: dict[str, str]) -> None:
    create = client.post(
        "/api/v1/system/clients", json={"client_id": "temp-client", "type": "bff"}, headers=admin_headers
    )
    api_key = create.json()["api_key"]

    revoke = client.delete("/api/v1/system/clients/temp-client", headers=admin_headers)
    assert revoke.status_code == 204

    resp = client.get("/api/v1/file/readme.md", headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 401


def test_rotate_api_key_invalidates_old_key(client: TestClient, admin_headers: dict[str, str]) -> None:
    create = client.post(
        "/api/v1/system/clients", json={"client_id": "rotate-me", "type": "bff"}, headers=admin_headers
    )
    old_key = create.json()["api_key"]

    rotate = client.post("/api/v1/system/clients/rotate-me/rotate-api-key", headers=admin_headers)
    assert rotate.status_code == 200
    new_key = rotate.json()["api_key"]
    assert new_key != old_key


def test_add_and_revoke_signing_key(client: TestClient, admin_headers: dict[str, str]) -> None:
    client.post(
        "/api/v1/system/clients", json={"client_id": "keyed-bff", "type": "bff", "issues_user_tokens": True}, headers=admin_headers
    )
    _priv, pub = generate_keypair_pem()

    add = client.post(
        "/api/v1/system/clients/keyed-bff/signing-keys",
        json={"kid": "k1", "public_key": pub},
        headers=admin_headers,
    )
    assert add.status_code == 201
    assert add.json()["active"] is True

    revoke = client.delete("/api/v1/system/clients/keyed-bff/signing-keys/k1", headers=admin_headers)
    assert revoke.status_code == 204

    after = client.get("/api/v1/system/clients/keyed-bff", headers=admin_headers).json()
    key_entry = next(k for k in after["signing_keys"] if k["kid"] == "k1")
    assert key_entry["active"] is False
    assert key_entry["revoked_at"] is not None


def test_403_clients_endpoint_for_non_admin(client: TestClient, admin: AdminContext) -> None:
    tester_headers = admin.headers_for("human:tester")

    resp = client.get("/api/v1/system/clients", headers=tester_headers)

    assert resp.status_code == 403


def test_403_create_client_for_non_admin(client: TestClient, admin: AdminContext) -> None:
    tester_headers = admin.headers_for("human:tester")

    resp = client.post(
        "/api/v1/system/clients", json={"client_id": "x", "type": "bff"}, headers=tester_headers
    )

    assert resp.status_code == 403


def test_autonomous_agent_creation_via_admin(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/system/clients",
        json={"client_id": "planner-03", "type": "autonomous-agent", "fixed_user_id": "system:planner-03"},
        headers=admin_headers,
    )

    assert resp.status_code == 201, resp.text
    api_key = resp.json()["api_key"]

    tree_resp = client.get("/api/v1/tree", headers={"Authorization": f"Bearer {api_key}"})
    assert tree_resp.status_code == 200


# ---- /system/audit (spec.md §9.4) ------------------------------------------------------------


def test_audit_query_filters_by_pii_only(client: TestClient, admin_headers: dict[str, str]) -> None:
    client.get("/api/v1/file/person.pii.json", headers=admin_headers)
    client.get("/api/v1/file/readme.md", headers=admin_headers)

    resp = client.get("/api/v1/system/audit", params={"pii_only": True}, headers=admin_headers)

    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert entries, "erwartete mindestens den PII-Read-Eintrag"
    assert all(e["pii_accessed"] is True for e in entries)
    assert all(e["path"] == "person.pii.json" for e in entries)


def test_audit_query_filters_by_user_id(client: TestClient, admin: AdminContext, admin_headers: dict[str, str]) -> None:
    tester_headers = admin.headers_for("human:tester")
    client.get("/api/v1/file/readme.md", headers=tester_headers)

    resp = client.get("/api/v1/system/audit", params={"user_id": "human:tester"}, headers=admin_headers)

    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert entries
    assert all(e["user_id"] == "human:tester" for e in entries)


def test_audit_query_filters_by_operation(client: TestClient, admin_headers: dict[str, str]) -> None:
    client.post(
        "/api/v1/file/readme.md/append",
        json={"content": "\nx", "if_version": client.get("/api/v1/file/readme.md", headers=admin_headers).json()["version"], "reason": "Test"},
        headers=admin_headers,
    )

    resp = client.get("/api/v1/system/audit", params={"operation": "append"}, headers=admin_headers)

    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert entries
    assert all(e["operation"] == "append" for e in entries)


def test_403_audit_endpoint_for_non_admin(client: TestClient, admin: AdminContext) -> None:
    tester_headers = admin.headers_for("human:tester")

    resp = client.get("/api/v1/system/audit", headers=tester_headers)

    assert resp.status_code == 403


# ---- query() nicht unterstützt bei jsonl/none-Backend (spec.md §9.4) ----------------------


@pytest.mark.parametrize("audit_backend", ["jsonl", "none"])
def test_audit_query_400_when_backend_does_not_support_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_backend: str
) -> None:
    repo_dir = tmp_path / "repo"
    init_repo(repo_dir)
    configure_env(monkeypatch, tmp_path, repo_dir, audit_backend=audit_backend)

    from agent_md_api.main import app

    with TestClient(app) as isolated_client:
        admin_ctx = bootstrap_admin(isolated_client)
        resp = isolated_client.get("/api/v1/system/audit", headers=admin_ctx.admin_headers)

        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["error"] == "invalid_request"
        assert "nicht unterstützt" in body["message"]

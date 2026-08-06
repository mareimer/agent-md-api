"""HTTP-Layer: POST /transaction (spec.md §6) — Atomarität, Fehlschlag-Isolation, ACL je Operation."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from conftest import AdminContext


def test_transaction_atomic_multi_op_success(client: TestClient, admin_headers: dict[str, str]) -> None:
    readme_version = client.get("/api/v1/file/readme.md", headers=admin_headers).json()["version"]
    data_version = client.get("/api/v1/file/data.json", headers=admin_headers).json()["version"]

    resp = client.post(
        "/api/v1/transaction",
        json={
            "reason": "Multi-Op-Test",
            "task_id": "t-1",
            "operations": [
                {"path": "neu/erstellt.md", "type": "write", "content": "neu", "if_version": None},
                {"path": "readme.md", "type": "edit", "old_str": "Initialer Inhalt.", "new_str": "geändert", "if_version": readme_version},
                {"path": "data.json", "type": "delete", "if_version": data_version},
            ],
        },
        headers=admin_headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["commit_id"]
    result_paths = {f["path"] for f in body["files"]}
    # Gelöschte Datei taucht laut GitStorage.run_transaction NICHT in files auf.
    assert result_paths == {"neu/erstellt.md", "readme.md"}

    assert client.get("/api/v1/file/neu/erstellt.md", headers=admin_headers).json()["content"] == "neu"
    assert "geändert" in client.get("/api/v1/file/readme.md", headers=admin_headers).json()["content"]
    assert client.get("/api/v1/file/data.json", headers=admin_headers).status_code == 404


def test_transaction_failure_leaves_all_operations_untouched(client: TestClient, admin_headers: dict[str, str]) -> None:
    readme_version = client.get("/api/v1/file/readme.md", headers=admin_headers).json()["version"]

    resp = client.post(
        "/api/v1/transaction",
        json={
            "reason": "Soll fehlschlagen",
            "operations": [
                {"path": "neu/darf-nicht-entstehen.md", "type": "write", "content": "x", "if_version": None},
                {"path": "readme.md", "type": "edit", "old_str": "Initialer Inhalt.", "new_str": "x", "if_version": readme_version},
                {"path": "data.json", "type": "delete", "if_version": "komplett-falsche-version"},
            ],
        },
        headers=admin_headers,
    )

    assert resp.status_code == 409, resp.text
    # Alle drei Operationen: nichts davon darf angewendet worden sein (Phase-1/Phase-2-Split, spec.md §6).
    assert client.get("/api/v1/file/neu/darf-nicht-entstehen.md", headers=admin_headers).status_code == 404
    unchanged = client.get("/api/v1/file/readme.md", headers=admin_headers).json()
    assert "Initialer Inhalt." in unchanged["content"]
    assert unchanged["version"] == readme_version
    assert client.get("/api/v1/file/data.json", headers=admin_headers).status_code == 200


def test_transaction_acl_checked_per_operation(client: TestClient, admin: AdminContext, admin_headers: dict[str, str]) -> None:
    """Eine Operation ist erlaubt, eine andere per ACL verboten -> die GESAMTE Transaktion
    wird abgelehnt (403), auch die für sich genommen erlaubte Operation wird nicht angewendet."""
    acl_rules = [{"user_id": "human:tester", "path_prefix": "gesperrt/", "read": "allow", "write": "deny"}]
    client.post("/api/v1/file/_system/acl.json", json={"content": json.dumps(acl_rules), "reason": "ACL"}, headers=admin_headers)
    client.post("/api/v1/file/gesperrt/geheim.md", json={"content": "geheim", "reason": "Setup"}, headers=admin_headers)
    gesperrt_version = client.get("/api/v1/file/gesperrt/geheim.md", headers=admin_headers).json()["version"]

    tester_headers = admin.headers_for("human:tester")
    resp = client.post(
        "/api/v1/transaction",
        json={
            "reason": "Mixed",
            "operations": [
                {"path": "erlaubter/neuer-pfad.md", "type": "write", "content": "erlaubt", "if_version": None},
                {"path": "gesperrt/geheim.md", "type": "edit", "old_str": "geheim", "new_str": "x", "if_version": gesperrt_version},
            ],
        },
        headers=tester_headers,
    )

    assert resp.status_code == 403
    assert resp.json()["error"] == "acl_denied"
    assert client.get("/api/v1/file/erlaubter/neuer-pfad.md", headers=admin_headers).status_code == 404


def test_transaction_acl_json_operation_requires_admin(
    client: TestClient, admin: AdminContext, admin_headers: dict[str, str]
) -> None:
    tester_headers = admin.headers_for("human:tester")

    resp = client.post(
        "/api/v1/transaction",
        json={"reason": "Versuch", "operations": [{"path": "_system/acl.json", "type": "write", "content": "[]", "if_version": None}]},
        headers=tester_headers,
    )

    assert resp.status_code == 403


def test_transaction_without_operations_is_rejected(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.post("/api/v1/transaction", json={"reason": "leer", "operations": []}, headers=admin_headers)

    assert resp.status_code == 400


def test_transaction_401_without_auth(client: TestClient) -> None:
    resp = client.post("/api/v1/transaction", json={"reason": "x", "operations": []})
    assert resp.status_code == 401

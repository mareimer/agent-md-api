"""AuditMiddleware (spec.md §9.2/§9.3) — genau ein Eintrag pro Request, korrekte Felder
(`reason`/`task_id`/`commit_id`/`pii_accessed`) für Lese- UND Schreibvorgänge.

Nutzt `GET /system/audit` (sqlite-Backend) als Prüfpunkt statt direktem DB-Zugriff —
das ist selbst der offizielle, spec-konforme Abfrageweg (spec.md §9.4) und hält den Test
nah an dem, was ein echter Aufrufer sähe.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from conftest import AdminContext


def _last_entry_for(client: TestClient, admin_headers: dict[str, str], *, operation: str) -> dict:
    resp = client.get("/api/v1/system/audit", params={"operation": operation}, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    entries = resp.json()["entries"]
    assert entries, f"kein Audit-Eintrag für operation={operation} gefunden"
    return entries[-1]


def test_edit_creates_audit_entry_with_reason_task_and_commit(client: TestClient, admin_headers: dict[str, str]) -> None:
    version = client.get("/api/v1/file/readme.md", headers=admin_headers).json()["version"]

    write_resp = client.post(
        "/api/v1/file/readme.md/edit",
        json={"old_str": "Initialer Inhalt.", "new_str": "x", "if_version": version, "reason": "Grund für den Edit", "task_id": "task-7"},
        headers=admin_headers,
    )
    commit_id = write_resp.json()["commit_id"]

    entry = _last_entry_for(client, admin_headers, operation="edit")
    assert entry["reason"] == "Grund für den Edit"
    assert entry["task_id"] == "task-7"
    assert entry["commit_id"] == commit_id
    assert entry["result"] == "success"
    assert entry["path"] == "readme.md"
    assert entry["user_id"] == "human:alice"
    assert entry["client_id"] == "web-bff"


def test_append_creates_audit_entry(client: TestClient, admin_headers: dict[str, str]) -> None:
    version = client.get("/api/v1/file/data.json", headers=admin_headers).json()["version"]

    client.post(
        "/api/v1/file/data.json/append",
        json={"content": "\nx", "if_version": version, "reason": "Append-Grund"},
        headers=admin_headers,
    )

    entry = _last_entry_for(client, admin_headers, operation="append")
    assert entry["reason"] == "Append-Grund"
    assert entry["task_id"] is None


def test_delete_creates_audit_entry(client: TestClient, admin_headers: dict[str, str]) -> None:
    version = client.get("/api/v1/file/data.json", headers=admin_headers).json()["version"]

    client.request(
        "DELETE", "/api/v1/file/data.json", json={"if_version": version, "reason": "Lösch-Grund"}, headers=admin_headers
    )

    entry = _last_entry_for(client, admin_headers, operation="delete")
    assert entry["reason"] == "Lösch-Grund"
    assert entry["result"] == "success"


def test_transaction_creates_single_audit_entry_covering_all_paths(client: TestClient, admin_headers: dict[str, str]) -> None:
    client.post(
        "/api/v1/transaction",
        json={
            "reason": "Multi",
            "task_id": "tx-1",
            "operations": [
                {"path": "a.md", "type": "write", "content": "a", "if_version": None},
                {"path": "b.md", "type": "write", "content": "b", "if_version": None},
            ],
        },
        headers=admin_headers,
    )

    entry = _last_entry_for(client, admin_headers, operation="transaction")
    assert set(entry["path"]) == {"a.md", "b.md"}
    assert entry["task_id"] == "tx-1"


def test_read_of_pii_json_sets_pii_accessed_true(client: TestClient, admin_headers: dict[str, str]) -> None:
    client.get("/api/v1/file/person.pii.json", headers=admin_headers)

    entry = _last_entry_for(client, admin_headers, operation="read")
    assert entry["path"] == "person.pii.json"
    assert entry["pii_accessed"] is True


def test_read_of_non_pii_file_sets_pii_accessed_false(client: TestClient, admin_headers: dict[str, str]) -> None:
    client.get("/api/v1/file/readme.md", headers=admin_headers)

    entry = _last_entry_for(client, admin_headers, operation="read")
    assert entry["path"] == "readme.md"
    assert entry["pii_accessed"] is False


def test_write_of_pii_json_sets_pii_accessed_true(client: TestClient, admin_headers: dict[str, str]) -> None:
    client.post(
        "/api/v1/file/neu.pii.json", json={"content": "{}", "reason": "PII anlegen"}, headers=admin_headers
    )

    entry = _last_entry_for(client, admin_headers, operation="write")
    assert entry["path"] == "neu.pii.json"
    assert entry["pii_accessed"] is True


def test_tree_read_creates_audit_entry(client: TestClient, admin_headers: dict[str, str]) -> None:
    client.get("/api/v1/tree", headers=admin_headers)

    entry = _last_entry_for(client, admin_headers, operation="tree")
    assert entry["result"] == "success"


def test_acl_denied_request_logged_with_result_denied(
    client: TestClient, admin: AdminContext, admin_headers: dict[str, str]
) -> None:
    acl_rules = [{"user_id": "human:tester", "path_prefix": "gesperrt/", "read": "allow", "write": "deny"}]
    client.post("/api/v1/file/_system/acl.json", json={"content": json.dumps(acl_rules), "reason": "ACL"}, headers=admin_headers)
    client.post("/api/v1/file/gesperrt/geheim.md", json={"content": "geheim", "reason": "Setup"}, headers=admin_headers)
    version = client.get("/api/v1/file/gesperrt/geheim.md", headers=admin_headers).json()["version"]

    tester_headers = admin.headers_for("human:tester")
    resp = client.post(
        "/api/v1/file/gesperrt/geheim.md/edit",
        json={"old_str": "geheim", "new_str": "x", "if_version": version, "reason": "Versuch"},
        headers=tester_headers,
    )
    assert resp.status_code == 403

    query_resp = client.get(
        "/api/v1/system/audit", params={"operation": "edit", "user_id": "human:tester"}, headers=admin_headers
    )
    entries = query_resp.json()["entries"]
    assert entries
    assert entries[-1]["result"] == "denied"

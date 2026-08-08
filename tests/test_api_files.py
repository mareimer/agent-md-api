"""HTTP-Layer: GET/POST/DELETE /file/{path}, .../edit, .../append (spec.md §4/§5/§8)."""

from __future__ import annotations

import json
from collections.abc import Callable

from fastapi.testclient import TestClient

from conftest import DUMMY_PDF_BYTES, AdminContext


def test_edit_endpoint_success(client: TestClient, admin_headers: dict[str, str]) -> None:
    get_resp = client.get("/api/v1/file/readme.md", headers=admin_headers)
    version = get_resp.json()["version"]

    resp = client.post(
        "/api/v1/file/readme.md/edit",
        json={"old_str": "Initialer Inhalt.", "new_str": "Geänderter Inhalt.", "if_version": version, "reason": "Test"},
        headers=admin_headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == "readme.md"
    assert body["version"] != version
    assert body["commit_id"]

    after = client.get("/api/v1/file/readme.md", headers=admin_headers).json()
    assert "Geänderter Inhalt." in after["content"]


def test_append_endpoint_success(client: TestClient, admin_headers: dict[str, str]) -> None:
    version = client.get("/api/v1/file/data.json", headers=admin_headers).json()["version"]

    resp = client.post(
        "/api/v1/file/data.json/append",
        json={"content": "\nangehängt", "if_version": version, "reason": "Test-Append"},
        headers=admin_headers,
    )

    assert resp.status_code == 200, resp.text
    after = client.get("/api/v1/file/data.json", headers=admin_headers).json()
    assert after["content"].endswith("angehängt")


def test_full_write_creates_new_file(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/file/neu/notiz.md",
        json={"content": "Ganz neu", "reason": "Neue Notiz"},
        headers=admin_headers,
    )

    assert resp.status_code == 200, resp.text
    after = client.get("/api/v1/file/neu/notiz.md", headers=admin_headers).json()
    assert after["content"] == "Ganz neu"


def test_delete_endpoint(client: TestClient, admin_headers: dict[str, str]) -> None:
    version = client.get("/api/v1/file/data.json", headers=admin_headers).json()["version"]

    resp = client.request(
        "DELETE",
        "/api/v1/file/data.json",
        json={"if_version": version, "reason": "Aufräumen"},
        headers=admin_headers,
    )

    assert resp.status_code == 204, resp.text
    assert client.get("/api/v1/file/data.json", headers=admin_headers).status_code == 404


def test_get_file_content_endpoint_for_binary(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.get("/api/v1/file/dummy.pdf/content", headers=admin_headers)

    assert resp.status_code == 200
    assert resp.content == DUMMY_PDF_BYTES
    assert resp.headers["content-type"] == "application/pdf"


def test_get_file_content_endpoint_rejects_text_kind(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.get("/api/v1/file/readme.md/content", headers=admin_headers)

    assert resp.status_code == 400
    assert resp.json()["error"] == "wrong_kind"


# ---- 401 ohne Auth ------------------------------------------------------------------------


def test_401_without_authorization_header(client: TestClient) -> None:
    resp = client.get("/api/v1/file/readme.md")
    assert resp.status_code == 401


def test_401_with_invalid_api_key(client: TestClient) -> None:
    resp = client.get("/api/v1/file/readme.md", headers={"Authorization": "Bearer nicht-existent"})
    assert resp.status_code == 401


# ---- 409 Versionskonflikt inkl. current_content ------------------------------------------


def test_409_version_conflict_includes_current_content(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/file/readme.md/edit",
        json={"old_str": "Initialer Inhalt.", "new_str": "x", "if_version": "veraltete-version", "reason": "Test"},
        headers=admin_headers,
    )

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "version_conflict"
    assert "Initialer Inhalt." in body["current_content"]
    assert body["current_version"]


# ---- 403 ACL-Verweigerung -------------------------------------------------------------------


def test_403_acl_denied_write(client: TestClient, admin: AdminContext, admin_headers: dict[str, str]) -> None:
    # Datei zuerst anlegen, solange noch keine acl.json existiert (Bootstrap-Allow) -- sonst
    # würde admin durch die gleich gesetzte, fail-closed ACL selbst ausgesperrt.
    create = client.post(
        "/api/v1/file/gesperrt/geheim.md", json={"content": "geheim", "reason": "Setup"}, headers=admin_headers
    )
    assert create.status_code == 200, create.text
    version = create.json()["version"]

    acl_rules = [{"user_id": "human:tester", "path_prefix": "gesperrt/", "read": "allow", "write": "deny"}]
    write_acl = client.post(
        "/api/v1/file/_system/acl.json",
        json={"content": json.dumps(acl_rules), "reason": "ACL: tester darf gesperrt/ nicht schreiben"},
        headers=admin_headers,
    )
    assert write_acl.status_code == 200, write_acl.text

    tester_headers = admin.headers_for("human:tester")

    resp = client.post(
        "/api/v1/file/gesperrt/geheim.md/edit",
        json={"old_str": "geheim", "new_str": "veröffentlicht", "if_version": version, "reason": "Versuch"},
        headers=tester_headers,
    )

    assert resp.status_code == 403
    assert resp.json()["error"] == "acl_denied"


def test_read_allowed_but_write_denied_by_acl(client: TestClient, admin: AdminContext, admin_headers: dict[str, str]) -> None:
    # Datei zuerst anlegen, solange noch keine acl.json existiert (Bootstrap-Allow).
    client.post("/api/v1/file/gesperrt/geheim.md", json={"content": "geheim", "reason": "Setup"}, headers=admin_headers)

    acl_rules = [{"user_id": "human:tester", "path_prefix": "gesperrt/", "read": "allow", "write": "deny"}]
    client.post(
        "/api/v1/file/_system/acl.json",
        json={"content": json.dumps(acl_rules), "reason": "ACL Setup"},
        headers=admin_headers,
    )

    tester_headers = admin.headers_for("human:tester")
    read_resp = client.get("/api/v1/file/gesperrt/geheim.md", headers=tester_headers)

    assert read_resp.status_code == 200


# ---- Binär-Upload via multipart ------------------------------------------------------------


def test_binary_upload_via_multipart(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/file/bilder/neu.pdf",
        files={"file": ("neu.pdf", b"%PDF-1.4\nneuer-inhalt\n", "application/pdf")},
        data={"reason": "Upload"},
        headers=admin_headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == "bilder/neu.pdf"

    download = client.get("/api/v1/file/bilder/neu.pdf/content", headers=admin_headers)
    assert download.content == b"%PDF-1.4\nneuer-inhalt\n"


def test_binary_upload_without_reason_rejected(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/file/bilder/ohne-reason.pdf",
        files={"file": ("ohne-reason.pdf", b"%PDF-1.4\n", "application/pdf")},
        headers=admin_headers,
    )

    assert resp.status_code == 400


def test_binary_upload_version_conflict(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/file/dummy.pdf",
        files={"file": ("dummy.pdf", b"ueberschrieben", "application/pdf")},
        data={"reason": "Ueberschreiben", "if_version": "falsche-version"},
        headers=admin_headers,
    )

    assert resp.status_code == 409


# ---- /_system/acl.json nur für Admin schreibbar, über den normalen /file-Endpunkt --------


def test_acl_json_not_writable_by_non_admin_via_normal_file_endpoint(
    client: TestClient, admin: AdminContext, admin_headers: dict[str, str]
) -> None:
    tester_headers = admin.headers_for("human:tester")

    resp = client.post(
        "/api/v1/file/_system/acl.json",
        json={"content": "[]", "reason": "Versuch als Nicht-Admin"},
        headers=tester_headers,
    )

    assert resp.status_code == 403
    assert resp.json()["error"] == "acl_denied"


def test_acl_json_writable_by_admin_via_normal_file_endpoint(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/file/_system/acl.json",
        json={"content": "[]", "reason": "Admin darf"},
        headers=admin_headers,
    )

    assert resp.status_code == 200, resp.text


def test_acl_json_delete_also_admin_only(client: TestClient, admin: AdminContext, admin_headers: dict[str, str]) -> None:
    write = client.post("/api/v1/file/_system/acl.json", json={"content": "[]", "reason": "Setup"}, headers=admin_headers)
    version = write.json()["version"]
    tester_headers = admin.headers_for("human:tester")

    resp = client.request(
        "DELETE", "/api/v1/file/_system/acl.json", json={"if_version": version, "reason": "Versuch"}, headers=tester_headers
    )

    assert resp.status_code == 403


# ---- GET /tree ACL-gefiltert (spec.md §4) --------------------------------------------------


def test_tree_hides_entries_without_read_permission(
    client: TestClient, admin: AdminContext, admin_headers: dict[str, str]
) -> None:
    # Testdatei zuerst anlegen, solange noch keine acl.json existiert (Bootstrap-Allow) --
    # sonst würde admin durch die gleich gesetzte, fail-closed ACL selbst ausgesperrt.
    client.post("/api/v1/file/geheim/akte.md", json={"content": "top secret", "reason": "Setup"}, headers=admin_headers)
    acl_rules = [
        {"user_id": "human:tester", "path_prefix": "/", "read": "allow"},
        {"user_id": "human:tester", "path_prefix": "geheim/", "read": "deny"},
    ]
    client.post("/api/v1/file/_system/acl.json", json={"content": json.dumps(acl_rules), "reason": "ACL"}, headers=admin_headers)

    tester_headers = admin.headers_for("human:tester")
    resp = client.get("/api/v1/tree", params={"depth": 3}, headers=tester_headers)

    assert resp.status_code == 200
    paths = [e["path"] for e in resp.json()["entries"]]
    assert "geheim/akte.md" not in paths
    assert "readme.md" in paths

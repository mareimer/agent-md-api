"""Ende-zu-Ende-Tests der WebDAV-Bridge (spec.md §14) — echte HTTP-WebDAV-Requests
(PROPFIND/GET/PUT/DELETE/MOVE/MKCOL/LOCK) gegen die volle Bridge-App (cheroot,
`webdav_bridge.main.build_app()`), die ihrerseits echte HTTP-Calls gegen eine echte,
laufende Agent-API-Instanz absetzt (Fixtures `bridge`/`bridge_http`, siehe conftest.py).

Kein Mock auf irgendeiner Ebene — genau das Zusammenspiel (wsgidav-Protokoll-Eigenheiten
+ Agent-API-Fehler-Mapping) ist der Zweck dieser Suite. Drei der hier abgedeckten Fälle
entsprechen 1:1 echten Bugs, die beim manuellen Testen gefunden und in `dav_provider.py`
behoben wurden (`_CapturingBytesIO`, `support_recursive_move`, idempotentes `delete()`
bei MOVE) — siehe Docstrings/Kommentare an den jeweiligen Stellen im Quellcode.
"""

from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET

import httpx

from tests.conftest import DUMMY_PDF_BYTES
from webdav_bridge.dav_provider import AUTO_REASON

_DAV_NS = {"d": "DAV:"}


def _propfind_hrefs(body: bytes) -> set[str]:
    root = ET.fromstring(body)
    return {href.text or "" for href in root.findall(".//d:response/d:href", _DAV_NS)}


# ---- Auth ---------------------------------------------------------------------------


def test_propfind_without_auth_is_401(bridge) -> None:
    with httpx.Client(base_url=bridge.base_url, timeout=10.0) as anon:
        r = anon.request("PROPFIND", "/", headers={"Depth": "1"})
    assert r.status_code == 401


def test_propfind_with_wrong_password_is_401(bridge) -> None:
    with httpx.Client(base_url=bridge.base_url, auth=(bridge.username, "definitiv-falsch"), timeout=10.0) as wrong:
        r = wrong.request("PROPFIND", "/", headers={"Depth": "1"})
    assert r.status_code == 401


# ---- PROPFIND / Tree-Struktur (spec.md §4 Depth-Bug) ---------------------------------


def test_propfind_root_lists_children_but_not_grandchildren(bridge_http: httpx.Client) -> None:
    r = bridge_http.request("PROPFIND", "/", headers={"Depth": "1"})

    assert r.status_code == 207
    hrefs = _propfind_hrefs(r.content)
    names = {h.rstrip("/").rsplit("/", 1)[-1] for h in hrefs}

    assert {"readme.md", "data.json", "person.pii.json", "dummy.pdf", "sub"} <= names
    assert "nested.md" not in names  # Enkelkind von root -> darf NICHT direkt erscheinen


# ---- GET ------------------------------------------------------------------------------


def test_get_text_file_returns_content_and_headers(bridge_http: httpx.Client) -> None:
    r = bridge_http.get("/readme.md")

    assert r.status_code == 200
    assert r.text == "# Testbaum\nInitialer Inhalt.\n"
    assert r.headers["content-type"].startswith("text/markdown")
    assert r.headers.get("etag")


def test_get_binary_file_returns_bytes_and_headers(bridge_http: httpx.Client) -> None:
    r = bridge_http.get("/dummy.pdf")

    assert r.status_code == 200
    assert r.content == DUMMY_PDF_BYTES
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers.get("etag")


def test_get_missing_file_is_404(bridge_http: httpx.Client) -> None:
    r = bridge_http.get("/nie-existiert.md")

    assert r.status_code == 404


# ---- PUT --------------------------------------------------------------------------------


def test_put_updates_existing_file_with_new_etag(bridge_http: httpx.Client) -> None:
    before = bridge_http.get("/readme.md")
    old_etag = before.headers["etag"]

    r = bridge_http.put("/readme.md", content="# Testbaum\nÜberschrieben über PUT.\n".encode("utf-8"))

    assert r.status_code in (200, 204)
    after = bridge_http.get("/readme.md")
    assert after.text == "# Testbaum\nÜberschrieben über PUT.\n"
    assert after.headers["etag"] != old_etag


def test_put_creates_new_file_returns_201(bridge_http: httpx.Client) -> None:
    r = bridge_http.put("/new-notes.md", content="Frisch angelegt.".encode("utf-8"))

    assert r.status_code == 201
    after = bridge_http.get("/new-notes.md")
    assert after.text == "Frisch angelegt."


def test_put_records_auto_reason_in_commit(bridge_http: httpx.Client, bridge) -> None:
    """spec.md §14.4: `reason` ist bei jedem Schreibvorgang automatisch generiert, da
    Explorer keine Eingabemöglichkeit dafür hat."""
    bridge_http.put("/reason-check.md", content="Inhalt für Reason-Check.".encode("utf-8"))

    last_commit = next(bridge.agent_api.repo.iter_commits())
    assert AUTO_REASON in last_commit.message


def test_put_with_stale_version_returns_409_not_hang_or_500(bridge_http: httpx.Client, bridge_agent_api_client, bridge) -> None:
    """Race-Fenster ausnutzen: wsgidav ruft `provider.get_resource_inst()` (der die Version
    einliest, spec.md §14.3 PUT-Zeile) VOR dem Body-Read auf. Ein Chunked-Body mit einer
    Pause in der Mitte hält dieses Fenster offen genug, um von AUSSEN (über einen zweiten,
    von der Bridge unabhängigen `AgentApiClient`) die Datei zwischenzeitlich zu ändern —
    genau das reale Szenario aus spec.md §14.5 ("Konfliktdatei entsteht wie bei jedem
    anderen Client"), deterministisch statt auf einen Zufalls-Interleave zwischen zwei
    Threads angewiesen zu sein."""
    body_continue = threading.Event()
    race_injected = threading.Event()

    def body_stream():
        yield b"# Erster Teil\n"
        assert race_injected.wait(timeout=10.0), "Race-Injektion nicht rechtzeitig erfolgt"
        yield b"Rest, der nie committet werden sollte.\n"

    result: dict[str, httpx.Response] = {}

    def do_put() -> None:
        result["response"] = bridge_http.put("/readme.md", content=body_stream())

    put_thread = threading.Thread(target=do_put)
    put_thread.start()

    # Kurze, aber ausreichende Wartezeit, bis der erste Chunk beim Server angekommen und
    # `get_resource_inst()` (Versions-Snapshot) sicher bereits gelaufen ist.
    time.sleep(0.3)

    current = bridge_agent_api_client.get_file(user_id=bridge.agent_api_user_id, path="readme.md")
    bridge_agent_api_client.write_text(
        user_id=bridge.agent_api_user_id,
        path="readme.md",
        content="Von unabhängiger Seite geändert.",
        if_version=current.version,
        reason="Race-Injektion für Konflikttest",
    )
    race_injected.set()

    put_thread.join(timeout=15.0)
    assert not put_thread.is_alive(), "PUT-Request ist gehängt statt mit 409 zu antworten."

    response = result["response"]
    assert response.status_code == 409, f"Erwartete 409, bekam {response.status_code}: {response.text}"

    # Der unabhängig geänderte Inhalt muss erhalten bleiben, der veraltete PUT darf nicht
    # durchgekommen sein (kein stilles Last-Write-Wins, spec.md §14.5).
    after = bridge_http.get("/readme.md")
    assert after.text == "Von unabhängiger Seite geändert."


# ---- DELETE -----------------------------------------------------------------------------


def test_delete_removes_file_then_404(bridge_http: httpx.Client) -> None:
    r = bridge_http.delete("/data.json")
    assert r.status_code in (200, 204)

    r2 = bridge_http.get("/data.json")
    assert r2.status_code == 404


# ---- MOVE (spec.md §14.3 Atomaritäts-Garantie + wsgidav-Quirks) -------------------------


def test_move_relocates_file_in_a_single_commit(bridge_http: httpx.Client, bridge) -> None:
    """Ziel liegt bewusst in einem bereits vorhandenen Verzeichnis (`sub/`, siehe
    `conftest.init_agent_api_repo`): Verzeichnisse existieren für die Agent-API nur
    implizit über bestehende Dateipfade (spec.md §14.3, `AgentApiCollectionResource`-
    Docstring) — ein MOVE in ein NOCH NICHT existierendes Verzeichnis scheitert bei
    wsgidav bereits VOR jedem Bridge-Code an dessen eigener Prüfung "Destination parent
    must be a collection" (409), weil `get_resource_inst()` für einen komplett neuen
    Verzeichnispfad `None` liefert. Das ist eine der in der Aufgabenstellung erwähnten
    Interpretations-würdigen Stellen — siehe Abschlussbericht."""
    commits_before = bridge.agent_api.commit_count()

    r = bridge_http.request(
        "MOVE",
        "/readme.md",
        headers={"Destination": f"{bridge.base_url}/sub/readme-moved.md"},
    )

    assert r.status_code in (201, 204), r.text

    src = bridge_http.get("/readme.md")
    assert src.status_code == 404

    dest = bridge_http.get("/sub/readme-moved.md")
    assert dest.status_code == 200
    assert dest.text == "# Testbaum\nInitialer Inhalt.\n"

    # Kernprüfung der Atomaritäts-Garantie: GENAU EIN neuer Agent-API-Commit, nicht zwei
    # (write+delete getrennt) — sichtbar an der Git-Historie des Test-Repos, nicht nur am
    # HTTP-Status. Das ist auch der Bugfix-Regressionstest für den `delete()`-Idempotenz-Fix:
    # ohne ihn würde wsgidavs redundanter zweiter delete()-Aufruf mit 404 scheitern.
    assert bridge.agent_api.commit_count() == commits_before + 1


def test_move_missing_source_is_404(bridge_http: httpx.Client) -> None:
    r = bridge_http.request(
        "MOVE",
        "/nie-existiert.md",
        headers={"Destination": f"{bridge_http.base_url}/moved/nie-existiert.md"},
    )
    assert r.status_code == 404


# ---- MKCOL (spec.md §14.3 No-Op) ---------------------------------------------------------


def test_mkcol_returns_201_without_touching_agent_api_tree(bridge_http: httpx.Client, bridge) -> None:
    commits_before = bridge.agent_api.commit_count()

    r = bridge_http.request("MKCOL", "/neuer-ordner")

    assert r.status_code == 201
    # Bewusster No-Op (spec.md §14.3): kein neuer Commit, keine Datei/Marker im Baum.
    assert bridge.agent_api.commit_count() == commits_before

    listing = bridge_http.request("PROPFIND", "/", headers={"Depth": "1"})
    names = {h.rstrip("/").rsplit("/", 1)[-1] for h in _propfind_hrefs(listing.content)}
    assert "neuer-ordner" not in names


# ---- LOCK (spec.md §14.3: nicht unterstützt) ---------------------------------------------


def test_lock_is_rejected_not_200(bridge_http: httpx.Client) -> None:
    lock_body = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<D:lockinfo xmlns:D="DAV:">'
        b"<D:lockscope><D:exclusive/></D:lockscope>"
        b"<D:locktype><D:write/></D:locktype>"
        b"<D:owner><D:href>test</D:href></D:owner>"
        b"</D:lockinfo>"
    )
    r = bridge_http.request("LOCK", "/readme.md", content=lock_body, headers={"Content-Type": "application/xml"})

    assert r.status_code >= 400
    assert r.status_code != 200

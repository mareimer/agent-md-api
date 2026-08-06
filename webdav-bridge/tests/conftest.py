"""Pytest-Fixtures für die WebDAV-Bridge-Testsuite (Auftrag: `docs/spec.md` §14).

Die Bridge ist ein reiner HTTP-Client der Agent-API (`webdav_bridge.agent_api_client.
AgentApiClient` spricht ausschließlich über echte HTTP-Requests mit der Agent-API) —
Mocken würde genau die Übersetzungslogik ungetestet lassen, um die es hier geht. Daher
starten die Fixtures unten eine ECHTE Agent-API-Instanz (uvicorn, `agent_md_api.main:app`)
in einem Hintergrund-Thread gegen einen frischen temporären Git-Arbeitsbaum, und für die
Ende-zu-Ende-HTTP-Tests zusätzlich die volle Bridge-App (cheroot, `webdav_bridge.main.
build_app()`) in einem zweiten Hintergrund-Thread.

Jeder Test bekommt sein eigenes Repo/seine eigene Bridge-Instanz (Function-Scope) —
keine geteilten Server zwischen Tests, damit Tests sich nicht gegenseitig stören
(insbesondere wichtig für den Ein-Instanz-Lock der Agent-API, spec.md §6).

Die Bridge importiert `agent_md_api` bewusst NICHT in ihrem Quellcode (siehe Kommentar
in `agent_api_client.py` — die beiden Deployables bleiben unabhängig). Diese Testsuite
darf es aber, sie testet ja gerade das Zusammenspiel beider Prozesse; `agent_md_api` ist
entsprechend nur eine Dev-/Test-Abhängigkeit (nicht in `webdav-bridge/pyproject.toml`
gelistet, siehe Kommentar dort).
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import git
import httpx
import pytest
import uvicorn
from cheroot import wsgi

from agent_md_api.auth.tokens import generate_keypair_pem
from agent_md_api.domain.models import ClientType
from agent_md_api.main import app as agent_app

from webdav_bridge.main import build_app
from webdav_bridge.user_access import UserAccessRegistry

DUMMY_PDF_BYTES = b"%PDF-1.4\n%webdav-bridge-test-dummy\n"
_START_TIMEOUT_SECONDS = 10.0


def _free_port() -> int:
    """Belegt kurz einen freien Port und gibt ihn wieder frei — kleines Race-Fenster,
    aber für lokale Tests ausreichend robust (analog zu gängigen Testserver-Mustern)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def init_agent_api_repo(root: Path) -> git.Repo:
    """Legt unter `root` einen frischen Git-Arbeitsbaum mit initialem Commit an — je eine
    Datei pro relevantem `kind` (spec.md §3) PLUS eine Unterverzeichnisebene mit einer
    verschachtelten Datei, damit sich die PROPFIND-Depth-Semantik (spec.md §4/§14.3, siehe
    STATUS.md-Bugfix zu `list_tree`) an echten Enkelkindern prüfen lässt."""
    root.mkdir(parents=True, exist_ok=True)
    repo = git.Repo.init(root)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Bridge Test Runner")
        cw.set_value("user", "email", "bridge-test@example.invalid")
    (root / "readme.md").write_text("# Testbaum\nInitialer Inhalt.\n", encoding="utf-8")
    (root / "data.json").write_text('{"key": "value"}', encoding="utf-8")
    (root / "person.pii.json").write_text('{"name": "Max Mustermann"}', encoding="utf-8")
    (root / "dummy.pdf").write_bytes(DUMMY_PDF_BYTES)
    (root / "sub").mkdir()
    (root / "sub" / "nested.md").write_text("# Verschachtelt\nNicht direkt unter root sichtbar.\n", encoding="utf-8")
    repo.index.add(["readme.md", "data.json", "person.pii.json", "dummy.pdf", "sub/nested.md"])
    repo.index.commit("initial: Testbaum aufgesetzt")
    return repo


@dataclass
class AgentApiInstance:
    """Handle auf eine laufende Agent-API-Testinstanz."""

    base_url: str
    repo_dir: Path
    repo: git.Repo
    admin_user_id: str

    def commit_count(self) -> int:
        return sum(1 for _ in self.repo.iter_commits())

    def register_bridge_client(self, *, client_id: str = "webdav-bridge", kid: str = "k1") -> tuple[str, str, str]:
        """Registriert einen Client mit `issues_user_tokens=True` + Signing-Key DIREKT über
        `agent_app.state.registry` (kein HTTP nötig — analog zu `bootstrap_admin` in der
        Kern-Testsuite, `tests/conftest.py`: der allererste Client kann nicht von einem
        Admin angelegt werden, der selbst erst über einen registrierten Client
        authentifizieren könnte). Gibt (api_key, kid, private_key_pem) zurück."""
        registry = agent_app.state.registry
        _entry, api_key = registry.create_client(client_id=client_id, type=ClientType.BFF, issues_user_tokens=True)
        private_key_pem, public_key_pem = generate_keypair_pem()
        registry.add_signing_key(client_id, kid=kid, public_key=public_key_pem)
        return api_key, kid, private_key_pem


def _wait_until(predicate, *, timeout: float = _START_TIMEOUT_SECONDS, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise RuntimeError("Timeout beim Warten auf Serverstart in einem Hintergrund-Thread.")


@pytest.fixture
def agent_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AgentApiInstance]:
    """Startet eine echte Agent-API-Instanz (uvicorn) gegen einen frischen temporären
    Git-Arbeitsbaum, Audit-Backend `none` (Auftrag: keine Audit-Fachlichkeit hier zu testen)."""
    repo_dir = tmp_path / "agent-repo"
    repo = init_agent_api_repo(repo_dir)
    admin_user_id = "human:test-admin"

    monkeypatch.setenv("AGENT_API_ROOT_DIR", str(repo_dir))
    monkeypatch.setenv("AGENT_API_AUDIT_BACKEND", "none")
    monkeypatch.setenv("AGENT_API_CLIENT_REGISTRY_PATH", str(tmp_path / "clients.json"))
    monkeypatch.setenv("AGENT_API_INSTANCE_LOCK_PATH", str(tmp_path / "instance.lock"))
    monkeypatch.setenv("AGENT_API_ADMIN_USER_ID", admin_user_id)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(agent_app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="agent-api-test-server", daemon=True)
    thread.start()

    _wait_until(lambda: server.started)
    with httpx.Client() as probe:
        def _reachable() -> bool:
            try:
                # unauthentifizierter Call reicht — 401 zeigt, dass die App bereits läuft
                # und ihre Dependencies (Storage/Registry) aus der Lifespan fertig sind.
                r = probe.get(f"{base_url}/api/v1/tree")
                return r.status_code in (401, 403)
            except httpx.TransportError:
                return False

        _wait_until(_reachable)

    try:
        yield AgentApiInstance(base_url=base_url, repo_dir=repo_dir, repo=repo, admin_user_id=admin_user_id)
    finally:
        server.should_exit = True
        thread.join(timeout=_START_TIMEOUT_SECONDS)


@dataclass
class BridgeStack:
    """Handle auf eine laufende Bridge-Testinstanz (cheroot) + den davor registrierten
    Testnutzer (spec.md §14.2 eigene Zugangsverwaltung)."""

    base_url: str
    agent_api: AgentApiInstance
    username: str
    password: str
    agent_api_user_id: str
    user_access_path: Path


@pytest.fixture
def bridge(agent_api: AgentApiInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[BridgeStack]:
    """Startet die volle Bridge-App (cheroot) gegen die laufende `agent_api`-Instanz, mit
    einem vorab registrierten Testnutzer (Basic-Auth, spec.md §14.2)."""
    api_key, kid, private_key_pem = agent_api.register_bridge_client()
    key_path = tmp_path / "bridge-signing-key.pem"
    key_path.write_text(private_key_pem, encoding="utf-8")

    user_access_path = tmp_path / "webdav-users.json"
    agent_api_user_id = "human:alice-test"
    # Testnutzer VOR build_app() anlegen: build_app() baut seine eigene
    # UserAccessRegistry-Instanz aus demselben Pfad und lädt beim Konstruieren die Datei
    # neu ein (spec.md §14.2) — testet nebenbei die Persistenz über einen Neuladen-Zyklus.
    bootstrap_registry = UserAccessRegistry(user_access_path)
    raw_pat = bootstrap_registry.create_user(username="marko", agent_api_user_id=agent_api_user_id)

    monkeypatch.setenv("WEBDAV_BRIDGE_AGENT_API_BASE_URL", agent_api.base_url)
    monkeypatch.setenv("WEBDAV_BRIDGE_AGENT_API_CLIENT_ID", "webdav-bridge")
    monkeypatch.setenv("WEBDAV_BRIDGE_AGENT_API_CLIENT_API_KEY", api_key)
    monkeypatch.setenv("WEBDAV_BRIDGE_SIGNING_KID", kid)
    monkeypatch.setenv("WEBDAV_BRIDGE_SIGNING_PRIVATE_KEY_PATH", str(key_path))
    monkeypatch.setenv("WEBDAV_BRIDGE_USER_ACCESS_PATH", str(user_access_path))

    app = build_app()
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    server = wsgi.Server(("127.0.0.1", port), app)
    thread = threading.Thread(target=server.start, name="webdav-bridge-test-server", daemon=True)
    thread.start()

    _wait_until(lambda: getattr(server, "ready", False))
    with httpx.Client() as probe:
        def _reachable() -> bool:
            try:
                r = probe.request("PROPFIND", f"{base_url}/", headers={"Depth": "0"})
                return r.status_code < 500
            except httpx.TransportError:
                return False

        _wait_until(_reachable)

    try:
        yield BridgeStack(
            base_url=base_url,
            agent_api=agent_api,
            username="marko",
            password=raw_pat,
            agent_api_user_id=agent_api_user_id,
            user_access_path=user_access_path,
        )
    finally:
        server.stop()
        thread.join(timeout=_START_TIMEOUT_SECONDS)


@pytest.fixture
def bridge_http(bridge: BridgeStack) -> Iterator[httpx.Client]:
    """Echter HTTP-Client gegen die volle Bridge-App, bereits mit gültigem Basic-Auth."""
    with httpx.Client(base_url=bridge.base_url, auth=(bridge.username, bridge.password), timeout=15.0) as client:
        yield client


@pytest.fixture
def bridge_agent_api_client(bridge: BridgeStack, tmp_path: Path):
    """`AgentApiClient` (aus `webdav_bridge.agent_api_client`), auf denselben registrierten
    Bridge-Client + dieselbe laufende Agent-API-Instanz konfiguriert wie `bridge` — nützlich,
    um in HTTP-Ende-zu-Ende-Tests unabhängig von der Bridge Seiteneffekte zu erzeugen
    (z.B. eine Race-Condition für den PUT-Konflikt-Test zu injizieren)."""
    from webdav_bridge.agent_api_client import AgentApiClient

    # Wiederverwendung desselben Signing-Keys, den `bridge` bereits für WEBDAV_BRIDGE_SIGNING_*
    # gesetzt hat (Env-Vars sind zu diesem Zeitpunkt durch die `bridge`-Fixture bereits gesetzt).
    import os

    return AgentApiClient(
        base_url=bridge.agent_api.base_url,
        client_id="webdav-bridge",
        client_api_key=os.environ["WEBDAV_BRIDGE_AGENT_API_CLIENT_API_KEY"],
        signing_kid=os.environ["WEBDAV_BRIDGE_SIGNING_KID"],
        signing_private_key_path=Path(os.environ["WEBDAV_BRIDGE_SIGNING_PRIVATE_KEY_PATH"]),
    )


@pytest.fixture
def agent_api_client(agent_api: AgentApiInstance, tmp_path: Path):
    """`AgentApiClient` direkt gegen `agent_api`, ohne die Bridge-HTTP-Schicht — für
    `test_agent_api_client.py` (isolierter Test des HTTP-Clients selbst, spec.md §14.2/§14.3)."""
    from webdav_bridge.agent_api_client import AgentApiClient

    api_key, kid, private_key_pem = agent_api.register_bridge_client(client_id="direct-client-test")
    key_path = tmp_path / "direct-client-signing-key.pem"
    key_path.write_text(private_key_pem, encoding="utf-8")

    return AgentApiClient(
        base_url=agent_api.base_url,
        client_id="direct-client-test",
        client_api_key=api_key,
        signing_kid=kid,
        signing_private_key_path=key_path,
    )

"""Pytest-Fixtures für die Agent-API-Testsuite.

Kernproblem, das alle Fixtures hier lösen: `agent_md_api.main.app` wird beim Modul-Import
nur als FastAPI-Objekt gebaut (`create_app()`), die `Settings` werden aber erst beim
Lifespan-Start gelesen (`main.lifespan()` -> `get_settings()` -> `Settings()` liest Env-Vars
zum Aufrufzeitpunkt, siehe config.py). Für isolierte Tests heißt das: Env-Vars müssen VOR
`with TestClient(app) as client:` gesetzt sein — dann liest jeder `with`-Block frische
Settings/Storage/Registry/Audit-Sink in `app.state`, ganz ohne `importlib.reload`.

Jeder Test bekommt einen frischen Git-Arbeitsbaum (tmp_path) mit initialen Dateien aller
relevanten `kind`s (md/json/pii.json/binary), damit sich Tests nicht gegenseitig stören.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import git
import pytest
from fastapi.testclient import TestClient

from agent_md_api.auth.tokens import generate_keypair_pem, mint_user_token
from agent_md_api.domain.models import ClientType
from agent_md_api.main import app as _app

DUMMY_PDF_BYTES = b"%PDF-1.4\n%agent-md-api-test-dummy\n"


def init_repo(root: Path) -> git.Repo:
    """Legt unter `root` einen frischen Git-Arbeitsbaum mit initialem Commit an —
    je eine Datei pro relevantem `kind` (spec.md §3), wiederverwendbar außerhalb
    der `repo_dir`-Fixture (z.B. für Tests mit abweichender Env-Konfiguration)."""
    root.mkdir(parents=True, exist_ok=True)
    repo = git.Repo.init(root)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test Runner")
        cw.set_value("user", "email", "test@example.invalid")
    (root / "readme.md").write_text("# Testbaum\nInitialer Inhalt.\n", encoding="utf-8")
    (root / "data.json").write_text('{"key": "value"}', encoding="utf-8")
    (root / "person.pii.json").write_text('{"name": "Max Mustermann"}', encoding="utf-8")
    (root / "dummy.pdf").write_bytes(DUMMY_PDF_BYTES)
    repo.index.add(["readme.md", "data.json", "person.pii.json", "dummy.pdf"])
    repo.index.commit("initial: Testbaum aufgesetzt")
    return repo


def configure_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo_dir: Path,
    *,
    audit_backend: str = "sqlite",
) -> dict[str, Path]:
    """Setzt alle `AGENT_API_*`-Env-Vars für einen isolierten Testlauf. Eigenständige
    Funktion (nicht nur Fixture), damit Tests mit abweichendem `audit_backend`
    (test_api_admin.py: jsonl/none) sie ohne Fixture-Umweg direkt aufrufen können."""
    paths = {
        "root_dir": repo_dir,
        "audit_db_path": tmp_path / "audit.db",
        "audit_log_path": tmp_path / "audit.jsonl",
        "client_registry_path": tmp_path / "clients.json",
        "instance_lock_path": tmp_path / "instance.lock",
    }
    monkeypatch.setenv("AGENT_API_ROOT_DIR", str(paths["root_dir"]))
    monkeypatch.setenv("AGENT_API_AUDIT_BACKEND", audit_backend)
    monkeypatch.setenv("AGENT_API_AUDIT_DB_PATH", str(paths["audit_db_path"]))
    monkeypatch.setenv("AGENT_API_AUDIT_LOG_PATH", str(paths["audit_log_path"]))
    monkeypatch.setenv("AGENT_API_CLIENT_REGISTRY_PATH", str(paths["client_registry_path"]))
    monkeypatch.setenv("AGENT_API_INSTANCE_LOCK_PATH", str(paths["instance_lock_path"]))
    monkeypatch.setenv("AGENT_API_ADMIN_USER_ID", "human:alice")
    return paths


class AdminContext:
    """Bootstrap-Ergebnis: ein `web-bff`-Client + Fähigkeit, User-Token für beliebige
    `user_id`s zu minten (gleiches Signing-Keypair, spec.md §10b)."""

    def __init__(self, *, client_id: str, api_key: str, private_key_pem: str, kid: str, admin_user_id: str) -> None:
        self.client_id = client_id
        self.api_key = api_key
        self.private_key_pem = private_key_pem
        self.kid = kid
        self.admin_user_id = admin_user_id

    def token_for(self, user_id: str, **kwargs: object) -> str:
        return mint_user_token(
            user_id=user_id,
            client_id=self.client_id,
            kid=self.kid,
            private_key_pem=self.private_key_pem,
            **kwargs,  # type: ignore[arg-type]
        )

    def headers_for(self, user_id: str, **kwargs: object) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "X-User-Token": self.token_for(user_id, **kwargs)}

    @property
    def admin_headers(self) -> dict[str, str]:
        return self.headers_for(self.admin_user_id)


def bootstrap_admin(client: TestClient) -> AdminContext:
    """Registriert `web-bff` DIREKT über `app.state.registry` (nicht über HTTP) — der
    allererste Client kann nicht von einem Admin angelegt werden, der selbst erst über
    einen registrierten Client authentifizieren könnte (Henne-Ei-Problem). Analog zum
    Ad-hoc-Verifikationsskript, das vor dieser Testsuite manuell benutzt wurde."""
    registry = client.app.state.registry  # type: ignore[attr-defined]
    settings = client.app.state.settings  # type: ignore[attr-defined]
    _entry, api_key = registry.create_client(client_id="web-bff", type=ClientType.BFF, issues_user_tokens=True)
    private_key_pem, public_key_pem = generate_keypair_pem()
    registry.add_signing_key("web-bff", kid="k1", public_key=public_key_pem)
    return AdminContext(
        client_id="web-bff",
        api_key=api_key,
        private_key_pem=private_key_pem,
        kid="k1",
        admin_user_id=settings.admin_user_id,
    )


# ---- Fixtures für den Regelfall (sqlite-Audit-Backend) --------------------------------


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    init_repo(root)
    return root


@pytest.fixture
def env(tmp_path: Path, repo_dir: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    return configure_env(monkeypatch, tmp_path, repo_dir)


@pytest.fixture
def client(env: dict[str, Path]) -> Iterator[TestClient]:
    with TestClient(_app) as c:
        yield c


@pytest.fixture
def admin(client: TestClient) -> AdminContext:
    return bootstrap_admin(client)


@pytest.fixture
def admin_headers(admin: AdminContext) -> dict[str, str]:
    return admin.admin_headers


@pytest.fixture
def make_autonomous_agent(
    client: TestClient, admin_headers: dict[str, str]
) -> Callable[[str, str], dict[str, str]]:
    """Legt über den echten HTTP-Endpunkt (als Admin) einen autonomen Agent-Client mit
    `fixed_user_id` an (spec.md §10) und gibt dessen Auth-Header zurück — kein
    `X-User-Token` nötig, da `fixed_user_id` direkt als Identität dient."""

    def _make(client_id: str, fixed_user_id: str) -> dict[str, str]:
        resp = client.post(
            "/api/v1/system/clients",
            json={"client_id": client_id, "type": "autonomous-agent", "fixed_user_id": fixed_user_id},
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text
        api_key = resp.json()["api_key"]
        return {"Authorization": f"Bearer {api_key}"}

    return _make

"""Bootstrap-CLI (agent_md_api.cli) — Rauchtests über die argparse-Oberfläche."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_md_api.cli import main


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    return tmp_path / "clients.json"


def test_create_client_prints_api_key(registry_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--registry-path", str(registry_path), "create-client", "web-bff"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "web-bff" in out
    assert "API-Key" in out

    data = json.loads(registry_path.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["client_id"] == "web-bff"
    assert data[0]["type"] == "bff"


def test_create_client_autonomous_agent_requires_fixed_user_id(
    registry_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(
        [
            "--registry-path",
            str(registry_path),
            "create-client",
            "planner",
            "--type",
            "autonomous-agent",
        ]
    )
    assert rc == 1
    assert "fixed_user_id" in capsys.readouterr().err


def test_create_client_autonomous_agent_with_fixed_user_id(
    registry_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(
        [
            "--registry-path",
            str(registry_path),
            "create-client",
            "planner",
            "--type",
            "autonomous-agent",
            "--fixed-user-id",
            "system:planner",
        ]
    )
    assert rc == 0
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    assert data[0]["fixed_user_id"] == "system:planner"


def test_list_clients_empty(registry_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--registry-path", str(registry_path), "list-clients"])
    assert rc == 0
    assert "Keine Clients" in capsys.readouterr().out


def test_list_clients_after_create(registry_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["--registry-path", str(registry_path), "create-client", "web-bff"])
    capsys.readouterr()
    rc = main(["--registry-path", str(registry_path), "list-clients"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "web-bff" in out
    assert "status=active" in out


def test_revoke_client(registry_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["--registry-path", str(registry_path), "create-client", "web-bff"])
    capsys.readouterr()
    rc = main(["--registry-path", str(registry_path), "revoke-client", "web-bff"])
    assert rc == 0

    data = json.loads(registry_path.read_text(encoding="utf-8"))
    assert data[0]["revoked"] is True


def test_rotate_key_changes_hash(registry_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["--registry-path", str(registry_path), "create-client", "web-bff"])
    capsys.readouterr()
    before = json.loads(registry_path.read_text(encoding="utf-8"))[0]["api_key_hash"]

    rc = main(["--registry-path", str(registry_path), "rotate-key", "web-bff"])
    assert rc == 0
    after = json.loads(registry_path.read_text(encoding="utf-8"))[0]["api_key_hash"]
    assert before != after


def test_revoke_unknown_client_returns_error(registry_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--registry-path", str(registry_path), "revoke-client", "does-not-exist"])
    assert rc == 1
    assert "nicht gefunden" in capsys.readouterr().err


def test_missing_registry_path_exits_with_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_API_CLIENT_REGISTRY_PATH", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        main(["create-client", "web-bff"])
    assert exc_info.value.code == 2

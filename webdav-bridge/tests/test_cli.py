"""Benutzerverwaltungs-CLI (webdav_bridge.cli) — Rauchtests über die argparse-Oberfläche."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from webdav_bridge.cli import main


@pytest.fixture
def users_path(tmp_path: Path) -> Path:
    return tmp_path / "webdav-users.json"


def test_create_user_prints_pat(users_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            "--users-path",
            str(users_path),
            "create-user",
            "marko",
            "--agent-api-user-id",
            "human:marko",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "marko" in out
    assert "Personal-Access-Token" in out

    data = json.loads(users_path.read_text(encoding="utf-8"))
    assert data["marko"]["agent_api_user_id"] == "human:marko"


def test_list_users_empty(users_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--users-path", str(users_path), "list-users"])
    assert rc == 0
    assert "Keine Benutzer" in capsys.readouterr().out


def test_list_users_after_create(users_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["--users-path", str(users_path), "create-user", "marko", "--agent-api-user-id", "human:marko"])
    main(["--users-path", str(users_path), "create-user", "anna", "--agent-api-user-id", "human:anna"])
    capsys.readouterr()

    rc = main(["--users-path", str(users_path), "list-users"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "marko" in out
    assert "anna" in out


def test_rotate_token_changes_hash(users_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["--users-path", str(users_path), "create-user", "marko", "--agent-api-user-id", "human:marko"])
    capsys.readouterr()
    before = json.loads(users_path.read_text(encoding="utf-8"))["marko"]["pat_hash"]

    rc = main(["--users-path", str(users_path), "rotate-token", "marko"])
    assert rc == 0
    after = json.loads(users_path.read_text(encoding="utf-8"))["marko"]["pat_hash"]
    assert before != after


def test_rotate_token_unknown_user_returns_error(users_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--users-path", str(users_path), "rotate-token", "unbekannt"])
    assert rc == 1
    assert "nicht gefunden" in capsys.readouterr().err


def test_revoke_user(users_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["--users-path", str(users_path), "create-user", "marko", "--agent-api-user-id", "human:marko"])
    capsys.readouterr()

    rc = main(["--users-path", str(users_path), "revoke-user", "marko"])
    assert rc == 0
    data = json.loads(users_path.read_text(encoding="utf-8"))
    assert "marko" not in data


def test_missing_users_path_exits_with_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEBDAV_BRIDGE_USER_ACCESS_PATH", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        main(["create-user", "marko", "--agent-api-user-id", "human:marko"])
    assert exc_info.value.code == 2

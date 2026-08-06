"""Tests für `webdav_bridge.user_access.UserAccessRegistry` (spec.md §14.2) — reine
Unit-Tests ohne laufende Server, die eigene, kleine Zugangsverwaltung der Bridge
(Benutzername -> agent_api_user_id + gehashtes Personal-Access-Token)."""

from __future__ import annotations

from pathlib import Path

import pytest

from webdav_bridge.user_access import UnknownUserError, UserAccessRegistry


def test_create_user_and_verify_succeeds(tmp_path: Path) -> None:
    registry = UserAccessRegistry(tmp_path / "users.json")
    raw_pat = registry.create_user(username="marko", agent_api_user_id="human:alice")

    assert registry.verify(username="marko", password=raw_pat) == "human:alice"


def test_verify_fails_with_wrong_password(tmp_path: Path) -> None:
    registry = UserAccessRegistry(tmp_path / "users.json")
    registry.create_user(username="marko", agent_api_user_id="human:alice")

    assert registry.verify(username="marko", password="definitiv-falsch") is None


def test_verify_fails_for_unknown_user(tmp_path: Path) -> None:
    registry = UserAccessRegistry(tmp_path / "users.json")

    assert registry.verify(username="unbekannt", password="irrelevant") is None


def test_create_user_returns_plaintext_pat_only_once(tmp_path: Path) -> None:
    """Die Registry selbst speichert nur den Hash (spec.md §14.2) — das Klartext-PAT ist
    nur der Rückgabewert von `create_user`/`rotate_token`, nirgends sonst abrufbar."""
    registry = UserAccessRegistry(tmp_path / "users.json")
    raw_pat = registry.create_user(username="marko", agent_api_user_id="human:alice")

    persisted = (tmp_path / "users.json").read_text(encoding="utf-8")
    assert raw_pat not in persisted


def test_rotate_token_invalidates_old_and_activates_new(tmp_path: Path) -> None:
    registry = UserAccessRegistry(tmp_path / "users.json")
    old_pat = registry.create_user(username="marko", agent_api_user_id="human:alice")

    new_pat = registry.rotate_token("marko")

    assert new_pat != old_pat
    assert registry.verify(username="marko", password=old_pat) is None
    assert registry.verify(username="marko", password=new_pat) == "human:alice"


def test_rotate_token_unknown_user_raises(tmp_path: Path) -> None:
    registry = UserAccessRegistry(tmp_path / "users.json")

    with pytest.raises(UnknownUserError):
        registry.rotate_token("nie-angelegt")


def test_revoke_user_removes_access(tmp_path: Path) -> None:
    registry = UserAccessRegistry(tmp_path / "users.json")
    raw_pat = registry.create_user(username="marko", agent_api_user_id="human:alice")

    registry.revoke_user("marko")

    assert registry.verify(username="marko", password=raw_pat) is None


def test_revoke_unknown_user_is_noop(tmp_path: Path) -> None:
    """`revoke_user` pop(..., None) — kein Fehler bei unbekanntem Benutzernamen (bewusst
    idempotent, analog zur DELETE-Idempotenz in dav_provider.py)."""
    registry = UserAccessRegistry(tmp_path / "users.json")

    registry.revoke_user("nie-angelegt")  # darf nicht werfen


def test_persistence_across_reload(tmp_path: Path) -> None:
    """Ein Prozess-Neustart lädt die Datei neu ein — das ist genau das Muster, das
    `webdav_bridge.main.build_app()` bei jedem Start durchläuft."""
    path = tmp_path / "users.json"
    first_registry = UserAccessRegistry(path)
    raw_pat = first_registry.create_user(username="marko", agent_api_user_id="human:alice")

    second_registry = UserAccessRegistry(path)

    assert second_registry.verify(username="marko", password=raw_pat) == "human:alice"


def test_persistence_across_reload_after_rotation(tmp_path: Path) -> None:
    path = tmp_path / "users.json"
    first_registry = UserAccessRegistry(path)
    first_registry.create_user(username="marko", agent_api_user_id="human:alice")
    new_pat = first_registry.rotate_token("marko")

    second_registry = UserAccessRegistry(path)

    assert second_registry.verify(username="marko", password=new_pat) == "human:alice"


def test_multiple_users_independent(tmp_path: Path) -> None:
    registry = UserAccessRegistry(tmp_path / "users.json")
    marko_pat = registry.create_user(username="marko", agent_api_user_id="human:alice")
    anna_pat = registry.create_user(username="anna", agent_api_user_id="human:anna")

    assert registry.verify(username="marko", password=marko_pat) == "human:alice"
    assert registry.verify(username="anna", password=anna_pat) == "human:anna"
    # PATs sind nicht gegeneinander austauschbar
    assert registry.verify(username="marko", password=anna_pat) is None

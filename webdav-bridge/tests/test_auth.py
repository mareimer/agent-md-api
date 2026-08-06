"""Tests für `webdav_bridge.auth.BridgeDomainController` (spec.md §14.2) — isoliert, ohne
laufenden WsgiDAV-Server: der DomainController wird direkt instanziiert, so wie wsgidav
das beim App-Start selbst tut (`http_authenticator.domain_controller` in `main.build_app()`)."""

from __future__ import annotations

from pathlib import Path

from webdav_bridge.auth import BridgeDomainController
from webdav_bridge.user_access import UserAccessRegistry


def _make_controller(tmp_path: Path) -> tuple[BridgeDomainController, UserAccessRegistry]:
    registry = UserAccessRegistry(tmp_path / "users.json")
    config = {"webdav_bridge.user_access_registry": registry}
    # wsgidav_app wird von BaseDomainController nur durchgereicht/gespeichert, nie
    # aufgerufen -- None reicht für einen isolierten Test dieser Klasse.
    controller = BridgeDomainController(None, config)
    return controller, registry


def test_get_domain_realm_is_constant(tmp_path: Path) -> None:
    controller, _ = _make_controller(tmp_path)

    assert controller.get_domain_realm("/irgendein/pfad", {}) == "agent-md-api-webdav-bridge"


def test_require_authentication_always_true(tmp_path: Path) -> None:
    """Kein anonymer Zugriff, jemals (spec.md §14.2 Kommentar in auth.py)."""
    controller, _ = _make_controller(tmp_path)

    assert controller.require_authentication("agent-md-api-webdav-bridge", {}) is True


def test_digest_auth_not_supported(tmp_path: Path) -> None:
    controller, _ = _make_controller(tmp_path)

    assert controller.supports_http_digest_auth() is False


def test_basic_auth_user_succeeds_and_sets_environ(tmp_path: Path) -> None:
    controller, registry = _make_controller(tmp_path)
    raw_pat = registry.create_user(username="marko", agent_api_user_id="human:alice")

    environ: dict = {}
    ok = controller.basic_auth_user("agent-md-api-webdav-bridge", "marko", raw_pat, environ)

    assert ok is True
    assert environ["webdav_bridge.agent_api_user_id"] == "human:alice"


def test_basic_auth_user_fails_with_wrong_password(tmp_path: Path) -> None:
    controller, registry = _make_controller(tmp_path)
    registry.create_user(username="marko", agent_api_user_id="human:alice")

    environ: dict = {}
    ok = controller.basic_auth_user("agent-md-api-webdav-bridge", "marko", "definitiv-falsch", environ)

    assert ok is False
    assert "webdav_bridge.agent_api_user_id" not in environ


def test_basic_auth_user_fails_for_unknown_user(tmp_path: Path) -> None:
    controller, _ = _make_controller(tmp_path)

    environ: dict = {}
    ok = controller.basic_auth_user("agent-md-api-webdav-bridge", "nie-angelegt", "irrelevant", environ)

    assert ok is False
    assert "webdav_bridge.agent_api_user_id" not in environ


def test_basic_auth_user_after_rotation_only_accepts_new_pat(tmp_path: Path) -> None:
    controller, registry = _make_controller(tmp_path)
    old_pat = registry.create_user(username="marko", agent_api_user_id="human:alice")
    new_pat = registry.rotate_token("marko")

    assert controller.basic_auth_user("agent-md-api-webdav-bridge", "marko", old_pat, {}) is False
    assert controller.basic_auth_user("agent-md-api-webdav-bridge", "marko", new_pat, {}) is True

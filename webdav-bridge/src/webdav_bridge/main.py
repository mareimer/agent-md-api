"""App-Wiring (spec.md §14.6 Tech-Stack-Vorschlag `wsgidav`). Start: `python -m webdav_bridge.main`."""

from __future__ import annotations

from wsgidav.wsgidav_app import WsgiDAVApp

from webdav_bridge.agent_api_client import AgentApiClient
from webdav_bridge.auth import BridgeDomainController
from webdav_bridge.config import Settings, get_settings
from webdav_bridge.dav_provider import AgentApiProvider
from webdav_bridge.user_access import UserAccessRegistry


def build_app(settings: Settings | None = None) -> WsgiDAVApp:
    settings = settings or get_settings()

    client = AgentApiClient(
        base_url=settings.agent_api_base_url,
        client_id=settings.agent_api_client_id,
        client_api_key=settings.agent_api_client_api_key,
        signing_kid=settings.signing_kid,
        signing_private_key_path=settings.signing_private_key_path,
        user_token_ttl_seconds=settings.user_token_ttl_seconds,
    )
    registry = UserAccessRegistry(settings.user_access_path)
    provider = AgentApiProvider(client)

    config = {
        "provider_mapping": {"/": provider},
        "verbose": 3,
        "lock_storage": False,  # LOCK/UNLOCK bewusst nicht unterstützt (spec.md §14.3)
        "property_manager": None,  # keine dead properties nötig
        "http_authenticator": {
            "domain_controller": BridgeDomainController,
            "accept_basic": True,
            "accept_digest": False,
            "default_to_digest": False,
        },
        "webdav_bridge.user_access_registry": registry,
        "dir_browser": {"enable": False},
    }
    return WsgiDAVApp(config)


def run() -> None:
    settings = get_settings()
    app = build_app(settings)
    # cheroot statt wsgiref: produktionstauglicher WSGI-Server, wsgidav-Dependency ohnehin vorhanden.
    from cheroot import wsgi

    server = wsgi.Server((settings.host, settings.port), app)
    print(f"WebDAV-Bridge läuft auf http://{settings.host}:{settings.port}")
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    run()

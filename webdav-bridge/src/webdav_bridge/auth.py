"""Basic-Auth-DomainController (spec.md §14.2) — einziges Verfahren, das Windows'
eingebauter WebDAV-Client beherrscht. Löst Benutzername+PAT über die eigene
`UserAccessRegistry` auf die Agent-API-`user_id` auf und legt sie in `environ` ab,
damit `dav_provider.AgentApiProvider` sie lesen kann, ohne selbst von der Registry
zu wissen (Trennung: Auth löst Identität auf, Provider nutzt sie nur)."""

from __future__ import annotations

from wsgidav.dc.base_dc import BaseDomainController

from webdav_bridge.user_access import UserAccessRegistry


class BridgeDomainController(BaseDomainController):
    def __init__(self, wsgidav_app, config) -> None:
        super().__init__(wsgidav_app, config)
        self._registry: UserAccessRegistry = config["webdav_bridge.user_access_registry"]

    def get_domain_realm(self, path_info, environ):
        return "agent-md-api-webdav-bridge"

    def require_authentication(self, realm, environ):
        return True  # kein anonymer Zugriff, jemals

    def supports_http_digest_auth(self):
        return False  # nur Basic — Explorer/Windows-WebDAV-Client, kein Digest nötig

    def basic_auth_user(self, realm, user_name, password, environ):
        user_id = self._registry.verify(username=user_name, password=password)
        if user_id is None:
            return False
        environ["webdav_bridge.agent_api_user_id"] = user_id
        return True

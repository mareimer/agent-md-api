"""Protokoll-Mapping WebDAV -> Agent-API (spec.md §14.3).

Registrierungsreihenfolge/Routing ist hier irrelevant (kein FastAPI-Router),
wsgidav ruft `get_resource_inst()` für jeden Pfad einzeln auf.

`reason` ist bei jedem Schreibvorgang automatisch generiert (spec.md §14.4) —
Explorer hat keine Eingabemöglichkeit dafür.
"""

from __future__ import annotations

import io

from wsgidav.dav_error import HTTP_CONFLICT, HTTP_FORBIDDEN, DAVError
from wsgidav.dav_provider import DAVCollection, DAVNonCollection, DAVProvider

from webdav_bridge.agent_api_client import (
    AgentApiClient,
    AgentApiConflictError,
    AgentApiForbiddenError,
    AgentApiNotFoundError,
)

AUTO_REASON = "Bearbeitet über WebDAV/Explorer"

_TEXT_KINDS = {"md", "json", "pii.json"}
_CONTENT_TYPE_BY_KIND = {"md": "text/markdown; charset=utf-8", "json": "application/json", "pii.json": "application/json"}


def classify_kind(path: str) -> str:
    """Dupliziert bewusst `agent_md_api.storage.git_repo.classify_kind` (spec.md-Kommentar
    in agent_api_client.py) — kein Cross-Package-Import zwischen den beiden Deployables."""
    if path.endswith(".pii.json"):
        return "pii.json"
    if path.endswith(".json"):
        return "json"
    if path.endswith(".md"):
        return "md"
    return "binary"


class _CapturingBytesIO(io.BytesIO):
    """`begin_write()`/`end_write()` (spec.md §14.3 PUT-Zeile): wsgidav schreibt in den von
    `begin_write()` zurückgegebenen Stream und ruft `close()` darauf auf, BEVOR
    `end_write()` aufgerufen wird — ein normales `io.BytesIO` wäre dann bereits
    geschlossen, `getvalue()` würde `ValueError` werfen. Sichert die Bytes daher
    in `close()` zusätzlich in ein Attribut, das nach dem Schließen lesbar bleibt."""

    def __init__(self) -> None:
        super().__init__()
        self.captured_value = b""

    def close(self) -> None:
        if not self.closed:
            self.captured_value = self.getvalue()
        super().close()


def _to_dav_error(exc: Exception) -> DAVError:
    if isinstance(exc, AgentApiConflictError):
        return DAVError(HTTP_CONFLICT, "Version veraltet — jemand anderes hat die Datei zwischenzeitlich geändert.", src_exception=exc)
    if isinstance(exc, AgentApiForbiddenError):
        return DAVError(HTTP_FORBIDDEN, str(exc), src_exception=exc)
    return DAVError(500, str(exc), src_exception=exc)


class AgentApiFileResource(DAVNonCollection):
    """Eine Datei (Text- oder Binär-Kind). `exists=False` repräsentiert eine noch
    nicht angelegte Datei (PUT auf neuen Pfad, spec.md §14.3 PUT-Zeile)."""

    def __init__(
        self, path: str, environ: dict, client: AgentApiClient, user_id: str,
        *, version: str | None, size: int | None, mime_type: str | None = None,
        content: str | None = None, exists: bool = True,
    ) -> None:
        super().__init__(path, environ)
        self._client = client
        self._user_id = user_id
        self._version = version
        self._size = size
        self._mime_type = mime_type
        self._content = content  # nur Text-Kinds, lazy falls None
        self._exists = exists
        self._kind = classify_kind(path)
        self._write_buffer: io.BytesIO | None = None

    @property
    def _api_path(self) -> str:
        return self.path.strip("/")

    def get_content_length(self):
        return self._size

    def get_content_type(self):
        if self._kind == "binary":
            return self._mime_type or "application/octet-stream"
        return _CONTENT_TYPE_BY_KIND[self._kind]

    def get_etag(self):
        return self._version

    def support_etag(self):
        return True

    def get_last_modified(self):
        return None  # nicht von der Agent-API exponiert (spec.md kennt kein Feld dafür)

    def get_content(self):
        try:
            if self._kind in _TEXT_KINDS:
                if self._content is None:
                    self._content = self._client.get_file(user_id=self._user_id, path=self._api_path).content
                return io.BytesIO((self._content or "").encode("utf-8"))
            data = self._client.get_file_content(user_id=self._user_id, path=self._api_path)
            return io.BytesIO(data)
        except (AgentApiConflictError, AgentApiForbiddenError) as exc:
            raise _to_dav_error(exc) from exc

    def begin_write(self, *, content_type=None):
        self._write_buffer = _CapturingBytesIO()
        return self._write_buffer

    def end_write(self, *, with_errors):
        # request_server.do_PUT ruft fileobj.close() VOR res.end_write() auf — ein normales
        # io.BytesIO wäre an dieser Stelle bereits geschlossen (getvalue() würde ValueError
        # werfen). _CapturingBytesIO sichert die Bytes beim close() in .captured_value.
        if with_errors or self._write_buffer is None:
            return
        data = self._write_buffer.captured_value
        self._write_buffer = None
        try:
            if self._kind in _TEXT_KINDS:
                content = data.decode("utf-8")
                self._version = self._client.write_text(
                    user_id=self._user_id, path=self._api_path, content=content,
                    if_version=self._version, reason=AUTO_REASON,
                )
                self._content = content
            else:
                self._version = self._client.write_binary(
                    user_id=self._user_id, path=self._api_path, data=data,
                    if_version=self._version, reason=AUTO_REASON,
                )
            self._size = len(data)
            self._exists = True
        except (AgentApiConflictError, AgentApiForbiddenError) as exc:
            raise _to_dav_error(exc) from exc

    def delete(self):
        """Idempotent bei "bereits weg" (spec.md §14.3-Erkenntnis): wsgidavs MOVE-Fallback
        ruft nach `copy_move_single()` bei is_move IMMER zusätzlich noch `delete()` auf der
        Quelle auf (request_server._copy_or_move, "copy/move file-by-file using copy/delete"),
        unabhängig davon, ob `copy_move_single()` die Quelle — wie hier für den atomaren
        Move-Fall — bereits selbst mitgelöscht hat. Ohne diese Toleranz würde JEDER Move
        mit einem 404 auf den zweiten, redundanten delete()-Aufruf scheitern, obwohl der
        gewünschte Endzustand (Quelle weg) längst erreicht ist."""
        try:
            self._client.delete(user_id=self._user_id, path=self._api_path, if_version=self._version, reason=AUTO_REASON)
        except AgentApiNotFoundError:
            return
        except (AgentApiConflictError, AgentApiForbiddenError) as exc:
            raise _to_dav_error(exc) from exc

    def support_recursive_move(self, dest_path):
        """wsgidav ruft das für JEDE MOVE-Quelle auf, nicht nur Collections (request_server
        `_copy_or_move`) — die Basisklasse `_DAVResource` nimmt aber `assert self.is_collection`
        an und `DAVNonCollection` überschreibt es nicht. False -> Standard-Fallback über
        `copy_move_single()` (das, was wir wollen — kein natives Recursive-Move nötig)."""
        return False

    def copy_move_single(self, dest_path, *, is_move):
        """MOVE/COPY (spec.md §14.3): Text atomar über /transaction, Binär bewusst
        NICHT atomar (drei Einzelaufrufe) — Limitation, nicht stillschweigend generalisiert."""
        dest_api_path = dest_path.strip("/")
        try:
            if self._kind in _TEXT_KINDS:
                content = self._content
                if content is None:
                    content = self._client.get_file(user_id=self._user_id, path=self._api_path).content
                if is_move:
                    self._client.move_text(
                        user_id=self._user_id, src_path=self._api_path, dest_path=dest_api_path,
                        content=content or "", src_version=self._version, reason=AUTO_REASON,
                    )
                else:
                    self._client.copy_text(user_id=self._user_id, dest_path=dest_api_path, content=content or "", reason=AUTO_REASON)
            else:
                data = self._client.get_file_content(user_id=self._user_id, path=self._api_path)
                self._client.write_binary(user_id=self._user_id, path=dest_api_path, data=data, if_version=None, reason=AUTO_REASON)
                if is_move:
                    self._client.delete(user_id=self._user_id, path=self._api_path, if_version=self._version, reason=AUTO_REASON)
        except (AgentApiConflictError, AgentApiForbiddenError, AgentApiNotFoundError) as exc:
            raise _to_dav_error(exc) from exc


class AgentApiCollectionResource(DAVCollection):
    """Ein Verzeichnis. Existiert für die Agent-API nur implizit über Dateipfade
    (spec.md §14.3) — MKCOL legt daher nichts an, DELETE/MOVE für ganze Verzeichnisse
    sind in dieser ersten Fassung bewusst NICHT unterstützt (Backlog, siehe STATUS.md);
    nur einzelne Dateien werden verschoben/gelöscht.

    Konsequenz (gefunden beim Testen, siehe tests/test_dav_provider_http.py
    `test_move_relocates_file_in_a_single_commit`): eine Datei per MOVE in ein noch
    NICHT existierendes Zielverzeichnis zu verschieben scheitert mit 409 — wsgidav
    prüft vor jedem MOVE selbst, ob `get_resource_inst()` für das Zielverzeichnis eine
    existierende Collection liefert, und lehnt sonst ab, BEVOR überhaupt Bridge-Code
    läuft. Funktioniert nur in bereits vorhandene Verzeichnisse hinein — Backlog."""

    def __init__(self, path: str, environ: dict, client: AgentApiClient, user_id: str) -> None:
        super().__init__(path, environ)
        self._client = client
        self._user_id = user_id

    @property
    def _api_path(self) -> str:
        return self.path.strip("/")

    def get_member_names(self):
        try:
            entries = self._client.list_tree(user_id=self._user_id, root=self._api_path, depth=1)
        except AgentApiForbiddenError as exc:
            raise _to_dav_error(exc) from exc
        own = self._api_path.rstrip("/")
        names = []
        for e in entries:
            if e.path.rstrip("/") == own:
                continue  # der Wurzeleintrag selbst, siehe spec.md §4 depth-Semantik
            names.append(e.path.rsplit("/", 1)[-1])
        return names

    def create_collection(self, name):
        """MKCOL — kein Agent-API-Äquivalent nötig, Erfolg ohne eigene Aktion (spec.md §14.3)."""
        child_path = f"{self.path.rstrip('/')}/{name}"
        return AgentApiCollectionResource(child_path, self.environ, self._client, self._user_id)

    def create_empty_resource(self, name):
        """PUT auf neuen Pfad (spec.md, request_server.do_PUT-Flow): liefert eine
        noch-nicht-existierende Ressource, die eigentliche Anlage passiert in
        `AgentApiFileResource.end_write()`."""
        child_path = f"{self.path.rstrip('/')}/{name}"
        return AgentApiFileResource(
            child_path, self.environ, self._client, self._user_id,
            version=None, size=0, exists=False,
        )


class AgentApiProvider(DAVProvider):
    def __init__(self, client: AgentApiClient) -> None:
        super().__init__()
        self._client = client

    def is_readonly(self):
        return False

    def get_resource_inst(self, path: str, environ: dict):
        user_id = environ.get("webdav_bridge.agent_api_user_id")
        if not user_id:
            return None  # keine Auth -> kein Zugriff; HTTPAuthenticator hat vorher schon abgelehnt

        if path in ("", "/"):
            return AgentApiCollectionResource("/", environ, self._client, user_id)

        api_path = path.strip("/")
        try:
            file_meta = self._client.get_file(user_id=user_id, path=api_path)
            return AgentApiFileResource(
                path, environ, self._client, user_id,
                version=file_meta.version, size=file_meta.size, mime_type=file_meta.mime_type,
                content=file_meta.content, exists=True,
            )
        except AgentApiNotFoundError:
            pass
        except AgentApiForbiddenError as exc:
            raise _to_dav_error(exc) from exc

        try:
            self._client.list_tree(user_id=user_id, root=api_path, depth=0)
            return AgentApiCollectionResource(path, environ, self._client, user_id)
        except AgentApiNotFoundError:
            return None
        except AgentApiForbiddenError as exc:
            raise _to_dav_error(exc) from exc

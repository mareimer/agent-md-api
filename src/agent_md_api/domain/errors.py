"""Domänenfehler — werden in api/ auf HTTP-Statuscodes gemappt (siehe openapi.yaml responses)."""

from __future__ import annotations


class AgentApiError(Exception):
    """Basisklasse. `error` ist der maschinenlesbare Code aus dem Error-Schema."""

    error: str = "error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(AgentApiError):
    error = "not_found"


class VersionConflictError(AgentApiError):
    """409 — trägt den aktuellen Inhalt/Version für die Konflikt-Response (spec.md §11)."""

    error = "version_conflict"

    def __init__(self, message: str, *, current_content: str | None, current_version: str) -> None:
        super().__init__(message)
        self.current_content = current_content
        self.current_version = current_version


class NoMatchError(AgentApiError):
    """old_str kommt 0-mal im aktuellen Inhalt vor (spec.md §5)."""

    error = "no_match"


class AmbiguousMatchError(AgentApiError):
    """old_str kommt >1-mal im aktuellen Inhalt vor (spec.md §5)."""

    error = "ambiguous_match"


class AclDeniedError(AgentApiError):
    error = "acl_denied"


class UnauthorizedError(AgentApiError):
    error = "unauthorized"


class ValidationError(AgentApiError):
    error = "invalid_request"


class WrongKindError(AgentApiError):
    """z.B. GET /file/{path}/content auf einer Text-Datei, oder str_replace auf binary."""

    error = "wrong_kind"

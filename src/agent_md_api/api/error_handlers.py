"""Mappt Domänenfehler auf HTTP-Responses passend zu openapi.yaml `components.responses`."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent_md_api.domain.errors import (
    AclDeniedError,
    AgentApiError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
    VersionConflictError,
)

_STATUS_BY_ERROR: dict[type[AgentApiError], int] = {
    UnauthorizedError: 401,
    AclDeniedError: 403,
    NotFoundError: 404,
    ValidationError: 400,
}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(VersionConflictError)
    async def _version_conflict(request: Request, exc: VersionConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "error": "version_conflict",
                "message": exc.message,
                "current_content": exc.current_content,
                "current_version": exc.current_version,
            },
        )

    @app.exception_handler(AgentApiError)
    async def _generic(request: Request, exc: AgentApiError) -> JSONResponse:
        status = _STATUS_BY_ERROR.get(type(exc), 400)
        return JSONResponse(status_code=status, content={"error": exc.error, "message": exc.message})

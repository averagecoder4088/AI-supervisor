"""Small, consistent API error model: ``{"error": <message>, "code": <CODE>}``."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

VALIDATION_ERROR = "VALIDATION_ERROR"
SUPERVISOR_NOT_FOUND = "SUPERVISOR_NOT_FOUND"
SUPERVISOR_VERSION_CONFLICT = "SUPERVISOR_VERSION_CONFLICT"
RUN_NOT_FOUND = "RUN_NOT_FOUND"
RUN_ALREADY_EXISTS = "RUN_ALREADY_EXISTS"
RUN_NOT_ACTIVE = "RUN_NOT_ACTIVE"
WORKFLOW_ALREADY_STARTED = "WORKFLOW_ALREADY_STARTED"
WORKFLOW_START_FAILED = "WORKFLOW_START_FAILED"
RUN_STATE_UPDATE_FAILED = "RUN_STATE_UPDATE_FAILED"
TEMPORAL_UNAVAILABLE = "TEMPORAL_UNAVAILABLE"


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _body(message: str, code: str) -> dict:
    return {"error": message, "code": code}


async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=_body(exc.message, exc.code))


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Request-schema problems are client errors: 400, not FastAPI's default 422.
    details = "; ".join(
        f"{'.'.join(str(p) for p in err.get('loc', ()) if p != 'body')}: {err.get('msg')}"
        for err in exc.errors()
    )
    return JSONResponse(status_code=400, content=_body(details or "Invalid request", VALIDATION_ERROR))


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(RequestValidationError, _validation_error)

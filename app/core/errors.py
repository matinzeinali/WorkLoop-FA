"""Standard API error format: {"error": {"code", "message", "details"}}."""

from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    BAD_REQUEST = "BAD_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    NAME_TAKEN = "NAME_TAKEN"
    PROFILE_IN_USE = "PROFILE_IN_USE"
    RUN_ACTIVE = "RUN_ACTIVE"
    NOT_NEEDS_USER = "NOT_NEEDS_USER"
    PROMPT_LOCKED = "PROMPT_LOCKED"
    PERMISSION_RESOLVED = "PERMISSION_RESOLVED"
    ROLES_NOT_SET = "ROLES_NOT_SET"
    PROJECT_NOT_SET = "PROJECT_NOT_SET"
    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
    NO_PENDING_PROMPTS = "NO_PENDING_PROMPTS"
    API_TEST_FAILED = "API_TEST_FAILED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_STATUS_TO_CODE: dict[int, ErrorCode] = {
    400: ErrorCode.BAD_REQUEST,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    422: ErrorCode.VALIDATION_ERROR,
}

Details = dict[str, Any] | list[Any] | None


class ApiError(Exception):
    """Raise from any endpoint to return the standard error body."""

    def __init__(
        self, code: ErrorCode, message: str, status_code: int = 400, details: Details = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details

    @classmethod
    def not_found(cls, what: str, item_id: int) -> "ApiError":
        return cls(ErrorCode.NOT_FOUND, f"{what} {item_id} not found.", 404, {"id": item_id})

    @classmethod
    def validation(cls, field: str, message: str) -> "ApiError":
        return cls(
            ErrorCode.VALIDATION_ERROR,
            "Request validation failed.",
            422,
            {"errors": [{"field": field, "message": message}]},
        )


def error_response(status_code: int, code: str, message: str, details: Details = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details}},
    )


def _field_path(loc: tuple[Any, ...]) -> str:
    # Drop the leading "body"/"query"/"path" part so clients get plain field names.
    parts = list(loc[1:]) if loc and loc[0] in {"body", "query", "path", "header"} else list(loc)
    return ".".join(str(p) for p in parts) or "body"


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                # A JSON syntax error has a character offset in loc, not a field name.
                "field": "body" if err["type"] == "json_invalid" else _field_path(tuple(err["loc"])),
                "message": err["msg"].removeprefix("Value error, "),
            }
            for err in exc.errors()
        ]
        return error_response(
            422, ErrorCode.VALIDATION_ERROR, "Request validation failed.", {"errors": errors}
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        message = exc.detail if isinstance(exc.detail, str) else code.replace("_", " ").title()
        return error_response(exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        details = {"type": type(exc).__name__} if get_settings().debug else None
        return error_response(500, ErrorCode.INTERNAL_ERROR, "Internal server error.", details)

"""Consistent API errors."""
from __future__ import annotations


class ApiError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str = "", *, code: str | None = None, status: int | None = None,
                 details: dict | None = None):
        super().__init__(message or self.code)
        self.message = message or self.code
        if code:
            self.code = code
        if status:
            self.status = status
        self.details = details or {}


class Unauthorized(ApiError):
    status, code = 401, "unauthorized"


class Forbidden(ApiError):
    status, code = 403, "forbidden"


class NotFound(ApiError):
    status, code = 404, "not_found"


class Conflict(ApiError):
    status, code = 409, "conflict"


class Unprocessable(ApiError):
    status, code = 422, "unprocessable"


class RateLimited(ApiError):
    status, code = 429, "rate_limited"


class CapacityExceeded(ApiError):
    status, code = 429, "capacity_exceeded"


class Unavailable(ApiError):
    status, code = 503, "service_unavailable"


class InvalidTransition(Exception):
    def __init__(self, current: str, target: str):
        super().__init__(f"invalid transition {current} -> {target}")
        self.current, self.target = current, target

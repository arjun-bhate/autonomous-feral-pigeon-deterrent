"""The one error shape every API response uses: ``{"error": {"code": ..., "message": ...}}``."""

from fastapi.responses import JSONResponse


class ApiError(Exception):
    """Raise this from a route to return the standard error shape."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)

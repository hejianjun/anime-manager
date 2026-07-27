from typing import Any


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Any = None,
        retryable: bool = False,
        status_code: int = 400,
    ) -> None:
        self.code = code
        self.message = message
        self.details = details
        self.retryable = retryable
        self.status_code = status_code
        super().__init__(message)


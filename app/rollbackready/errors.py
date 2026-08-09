from __future__ import annotations

from typing import Any


class RollbackReadyError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        analysis_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.analysis_id = analysis_id
        self.details = details or {}


def not_found(analysis_id: str) -> RollbackReadyError:
    return RollbackReadyError(
        "ANALYSIS_NOT_FOUND",
        "The requested analysis does not exist.",
        status_code=404,
        analysis_id=analysis_id,
    )

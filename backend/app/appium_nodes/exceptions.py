from __future__ import annotations

from app.core.errors import AppError


class NodeManagerError(AppError):
    status_code = 400
    code = "VALIDATION_ERROR"


class NodePortConflictError(NodeManagerError):
    pass

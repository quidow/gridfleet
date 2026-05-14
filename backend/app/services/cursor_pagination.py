"""Legacy import shim for Phase 0b backend domain-layout refactor.

Real implementation lives at ``app/core/pagination.py``. Phase 16 deletes
this shim once every caller migrates.
"""

from app.core.pagination import (
    CursorPage,
    CursorPaginationError,
    CursorToken,
    decode_cursor,
    encode_cursor,
)

__all__ = [
    "CursorPage",
    "CursorPaginationError",
    "CursorToken",
    "decode_cursor",
    "encode_cursor",
]

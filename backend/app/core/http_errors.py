"""Shared router helpers converting lookup failures to HTTP 404.

Three shapes, one helper each:
- ``found_or_404`` for the ``if thing is None: raise HTTPException(404, ...)`` check.
- ``convert_not_found`` for ``except KeyError/LookupError/NoResultFound -> 404``
  conversion; with no explicit detail it preserves the legacy ``detail=str(exc)``
  response bodies. Scope it to a lookup — it is too wide to wrap a command body.
- ``convert_missing_row`` for the same conversion narrowed to ``NoResultFound``,
  for routers that wrap a whole command in the conversion.
"""

from contextlib import contextmanager
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy.exc import NoResultFound

if TYPE_CHECKING:
    from collections.abc import Iterator


def found_or_404[T](value: T | None, detail: str) -> T:
    if value is None:
        raise HTTPException(status_code=404, detail=detail)
    return value


@contextmanager
def convert_not_found(detail: str | None = None) -> Iterator[None]:
    try:
        yield
    except (LookupError, NoResultFound) as exc:
        # LookupError covers KeyError/IndexError; sqlalchemy's NoResultFound is a
        # separate hierarchy. str(exc) keeps byte-for-byte parity with the legacy
        # ``detail=str(e)`` sites when no explicit detail is given.
        raise HTTPException(status_code=404, detail=detail if detail is not None else str(exc)) from exc


@contextmanager
def convert_missing_row(detail: str) -> Iterator[None]:
    """Translate "the row the command wanted to lock is gone" into a 404.

    Deliberately narrower than :func:`convert_not_found`: a router that owns a
    command boundary wraps the whole command body, and ``convert_not_found``
    would also swallow every ``KeyError``/``IndexError`` raised anywhere inside
    it — turning a bug into ``404 {"detail": "Device not found"}`` instead of the
    500 it is. ``NoResultFound`` is what the aggregate-lock helpers raise for a
    genuinely missing row.
    """
    try:
        yield
    except NoResultFound as exc:
        raise HTTPException(status_code=404, detail=detail) from exc

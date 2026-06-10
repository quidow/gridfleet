"""Shared router helpers converting lookup failures to HTTP 404.

Two shapes, one helper each:
- ``found_or_404`` for the ``if thing is None: raise HTTPException(404, ...)`` check.
- ``convert_not_found`` for ``except KeyError/LookupError/NoResultFound -> 404``
  conversion; with no explicit detail it preserves the legacy ``detail=str(exc)``
  response bodies.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy.exc import NoResultFound


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

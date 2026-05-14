"""Smoke tests for app.dependencies shared aliases."""

from typing import get_args

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app import dependencies as deps
from app.database import get_db
from app.services.auth_dependencies import require_admin


def _unwrap(alias: object) -> tuple[type, object]:
    args = get_args(alias)
    return args[0], args[1]


def test_db_dep_wraps_get_db() -> None:
    typ, dep = _unwrap(deps.DbDep)
    assert typ is AsyncSession
    assert isinstance(dep, type(Depends(get_db)))
    assert dep.dependency is get_db


def test_admin_dep_wraps_require_admin() -> None:
    typ, dep = _unwrap(deps.AdminDep)
    assert typ is str
    assert dep.dependency is require_admin

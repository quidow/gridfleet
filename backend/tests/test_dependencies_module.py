"""Smoke tests for app.dependencies shared aliases."""

from typing import get_args

from fastapi import Depends
from fastapi.params import Depends as DependsParam
from sqlalchemy.ext.asyncio import AsyncSession

from app import dependencies as deps
from app.auth.dependencies import require_admin
from app.core.database import get_db


def _unwrap(alias: object) -> tuple[type, DependsParam]:
    args = get_args(alias)
    typ, dep = args[0], args[1]
    assert isinstance(dep, DependsParam)
    return typ, dep


def test_db_dep_wraps_get_db() -> None:
    typ, dep = _unwrap(deps.DbDep)
    assert typ is AsyncSession
    assert isinstance(dep, type(Depends(get_db)))
    assert dep.dependency is get_db


def test_admin_dep_wraps_require_admin() -> None:
    typ, dep = _unwrap(deps.AdminDep)
    assert typ is str
    assert dep.dependency is require_admin

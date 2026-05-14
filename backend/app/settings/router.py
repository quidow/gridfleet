from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.dependencies import DbDep  # noqa: TC001 - FastAPI inspects dependency aliases at runtime.
from app.settings import settings_service
from app.settings.schemas import SettingRead, SettingsBulkUpdate, SettingsGrouped, SettingUpdate

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("", response_model=list[SettingsGrouped])
async def list_settings() -> list[dict[str, Any]]:
    return settings_service.get_all_grouped()


@router.put("/bulk", response_model=list[SettingRead])
async def bulk_update_settings(
    body: SettingsBulkUpdate,
    db: DbDep,
) -> list[dict[str, Any]]:
    try:
        return await settings_service.bulk_update(db, body.settings)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/reset-all")
async def reset_all_settings(db: DbDep) -> dict[str, str]:
    await settings_service.reset_all(db)
    return {"status": "all settings reset to defaults"}


@router.get("/{key:path}", response_model=SettingRead)
async def get_setting(key: str) -> dict[str, Any]:
    try:
        return settings_service.get_setting_response(key)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.put("/{key:path}", response_model=SettingRead)
async def update_setting(
    key: str,
    body: SettingUpdate,
    db: DbDep,
) -> dict[str, Any]:
    try:
        return await settings_service.update(db, key, body.value)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/reset/{key:path}", response_model=SettingRead)
async def reset_setting(
    key: str,
    db: DbDep,
) -> dict[str, Any]:
    try:
        return await settings_service.reset(db, key)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

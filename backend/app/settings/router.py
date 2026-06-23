from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.core.dependencies import DbDep
from app.core.http_errors import convert_not_found
from app.events.dependencies import EventServicesDep
from app.settings.dependencies import SettingsServicesDep
from app.settings.schemas import SettingRead, SettingsBulkUpdate, SettingsGrouped, SettingUpdate

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("", response_model=list[SettingsGrouped])
async def list_settings(settings_services: SettingsServicesDep) -> list[dict[str, Any]]:
    return settings_services.service.get_all_grouped()


@router.put("/bulk", response_model=list[SettingRead])
async def bulk_update_settings(
    body: SettingsBulkUpdate,
    db: DbDep,
    settings_services: SettingsServicesDep,
    events: EventServicesDep,
) -> list[dict[str, Any]]:
    try:
        return await settings_services.service.bulk_update(db, body.settings, publisher=events.publisher)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/reset-all")
async def reset_all_settings(
    db: DbDep, settings_services: SettingsServicesDep, events: EventServicesDep
) -> dict[str, str]:
    await settings_services.service.reset_all(db, publisher=events.publisher)
    return {"status": "all settings reset to defaults"}


@router.get("/{key:path}", response_model=SettingRead)
async def get_setting(key: str, settings_services: SettingsServicesDep) -> dict[str, Any]:
    with convert_not_found():
        return settings_services.service.get_setting_response(key)


@router.put("/{key:path}", response_model=SettingRead)
async def update_setting(
    key: str,
    body: SettingUpdate,
    db: DbDep,
    settings_services: SettingsServicesDep,
    events: EventServicesDep,
) -> dict[str, Any]:
    try:
        return await settings_services.service.update(db, key, body.value, publisher=events.publisher)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/reset/{key:path}", response_model=SettingRead)
async def reset_setting(
    key: str,
    db: DbDep,
    settings_services: SettingsServicesDep,
    events: EventServicesDep,
) -> dict[str, Any]:
    with convert_not_found():
        return await settings_services.service.reset(db, key, publisher=events.publisher)

"""Settings-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from app.settings.services_container import SettingsServices


def get_settings_services(request: Request) -> SettingsServices:
    return request.app.state.services.settings  # type: ignore[no-any-return]


SettingsServicesDep = Annotated["SettingsServices", Depends(get_settings_services)]

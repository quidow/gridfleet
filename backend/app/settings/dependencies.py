"""Settings-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends

from app.core.service_deps import make_services_getter

if TYPE_CHECKING:
    from app.settings.services_container import SettingsServices

get_settings_services = make_services_getter("settings")
SettingsServicesDep = Annotated["SettingsServices", Depends(get_settings_services)]

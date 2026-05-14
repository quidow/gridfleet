import sys

from app.appium_nodes.routers import admin as _admin
from app.appium_nodes.routers.admin import *  # noqa: F403

sys.modules[__name__] = _admin

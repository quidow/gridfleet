import sys

from app.devices.routers import groups as _groups
from app.devices.routers.groups import *  # noqa: F403

sys.modules[__name__] = _groups

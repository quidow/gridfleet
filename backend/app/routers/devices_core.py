import sys

from app.devices.routers import core as _core
from app.devices.routers.core import *  # noqa: F403

sys.modules[__name__] = _core

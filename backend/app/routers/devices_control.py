import sys

from app.devices.routers import control as _control
from app.devices.routers.control import *  # noqa: F403

sys.modules[__name__] = _control

import sys

from app.devices.routers import helpers as _helpers
from app.devices.routers.helpers import *  # noqa: F403

sys.modules[__name__] = _helpers

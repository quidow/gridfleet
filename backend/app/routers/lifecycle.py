import sys

from app.devices.routers import lifecycle_incidents as _lifecycle_incidents
from app.devices.routers.lifecycle_incidents import *  # noqa: F403

sys.modules[__name__] = _lifecycle_incidents

import sys

from app.devices.services import lifecycle_incidents as _lifecycle_incidents
from app.devices.services.lifecycle_incidents import *  # noqa: F403

sys.modules[__name__] = _lifecycle_incidents

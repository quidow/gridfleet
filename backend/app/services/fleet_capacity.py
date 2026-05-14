import sys

from app.devices.services import fleet_capacity as _fleet_capacity
from app.devices.services.fleet_capacity import *  # noqa: F403

sys.modules[__name__] = _fleet_capacity

import sys

from app.devices.services import maintenance as _maintenance
from app.devices.services.maintenance import *  # noqa: F403

sys.modules[__name__] = _maintenance

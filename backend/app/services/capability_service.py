import sys

from app.devices.services import capability as _capability
from app.devices.services.capability import *  # noqa: F403

sys.modules[__name__] = _capability

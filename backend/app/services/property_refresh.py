import sys

from app.devices.services import property_refresh as _property_refresh
from app.devices.services.property_refresh import *  # noqa: F403

sys.modules[__name__] = _property_refresh

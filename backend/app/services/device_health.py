import sys

from app.devices.services import health as _health
from app.devices.services.health import *  # noqa: F403

sys.modules[__name__] = _health

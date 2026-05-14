import sys

from app.devices.services import health_view as _health_view
from app.devices.services.health_view import *  # noqa: F403

sys.modules[__name__] = _health_view

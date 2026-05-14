import sys

from app.devices.services import state as _state
from app.devices.services.state import *  # noqa: F403

sys.modules[__name__] = _state

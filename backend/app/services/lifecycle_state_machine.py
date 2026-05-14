import sys

from app.devices.services import lifecycle_state_machine as _lifecycle_state_machine
from app.devices.services.lifecycle_state_machine import *  # noqa: F403

sys.modules[__name__] = _lifecycle_state_machine

import sys

from app.devices.services import lifecycle_state_machine_types as _lifecycle_state_machine_types
from app.devices.services.lifecycle_state_machine_types import *  # noqa: F403

sys.modules[__name__] = _lifecycle_state_machine_types

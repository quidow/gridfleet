import sys

from app.devices.services import lifecycle_state_machine_hooks as _lifecycle_state_machine_hooks
from app.devices.services.lifecycle_state_machine_hooks import *  # noqa: F403

sys.modules[__name__] = _lifecycle_state_machine_hooks

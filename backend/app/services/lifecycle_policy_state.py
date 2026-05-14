import sys

from app.devices.services import lifecycle_policy_state as _lifecycle_policy_state
from app.devices.services.lifecycle_policy_state import *  # noqa: F403

sys.modules[__name__] = _lifecycle_policy_state

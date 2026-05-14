import sys

from app.devices.services import lifecycle_policy_actions as _lifecycle_policy_actions
from app.devices.services.lifecycle_policy_actions import *  # noqa: F403

sys.modules[__name__] = _lifecycle_policy_actions

import sys

from app.devices.services import lifecycle_policy_summary as _lifecycle_policy_summary
from app.devices.services.lifecycle_policy_summary import *  # noqa: F403

sys.modules[__name__] = _lifecycle_policy_summary

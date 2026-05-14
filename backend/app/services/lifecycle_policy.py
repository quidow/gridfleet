import sys

from app.devices.services import lifecycle_policy as _lifecycle_policy
from app.devices.services.lifecycle_policy import *  # noqa: F403

sys.modules[__name__] = _lifecycle_policy
